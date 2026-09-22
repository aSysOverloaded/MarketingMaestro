"""Compile the brochure HTML (Jinja2) from the pipeline's results."""
import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.catalog import Product, Recommendation
from app.config import SERVICE_DIR, settings

# The cover is a fixed-height A4 page with overflow hidden - anything past this many
# paragraphs would be clipped silently in the PDF.
MAX_COVER_PARAGRAPHS = 3

_env = Environment(
    loader=FileSystemLoader(SERVICE_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
)


@dataclass
class Brand:
    name: str
    primary_color: str
    secondary_color: str
    initial: str


DEFAULT_BRAND = Brand("Premium Home", "#1e3a8a", "#0f172a", "P")

# Match brand names only as standalone words: an earlier substring check for "wash"/"dryer"
# branded every dishwasher as LG. Product categories are not brands.
_BRANDS = [
    (re.compile(r"\b(samsung|nq70|nv51|rf28|dw80)\b"), Brand("Samsung", "#1428a0", "#000000", "S")),
    (re.compile(r"\blg\b"), Brand("LG Electronics", "#a50034", "#3c3c3c", "L")),
]

# Stock-photo fallback when a catalog page yielded no image of its own. Only used when the
# product's category clearly matches one of these; a generic "nice photo" is worse than none,
# because a kitchen picture in a sports brochure reads as a mistake to the customer.
_STOCK_IMAGES = [
    (("refrigerator", "fridge"), "https://images.unsplash.com/photo-1588854337236-6889d631faa8?auto=format&fit=crop&q=80&w=800"),
    (("dishwasher",), "https://images.unsplash.com/photo-1581578731548-c64695cc6952?auto=format&fit=crop&q=80&w=800"),
    (("washer", "washing", "dryer"), "https://images.unsplash.com/photo-1626806787461-102c1bfaaea1?auto=format&fit=crop&q=80&w=800"),
    (("range", "oven", "stove", "cooktop", "microwave"), "https://images.unsplash.com/photo-1590794056226-79ef3a8147e1?auto=format&fit=crop&q=80&w=800"),
]

# Symbols for the currencies a catalog is likely to print; anything else is shown as a code
# ("CHF 59.00"), which is correct if less pretty.
_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "INR": "₹", "JPY": "¥"}

_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


def brand_for_model(model: str) -> Brand:
    lower = model.lower()
    for pattern, brand in _BRANDS:
        if pattern.search(lower):
            return brand
    return DEFAULT_BRAND


def brand_from_catalog(catalog_brand: Optional[dict]) -> Optional[Brand]:
    """The brand detected from the catalog at ingest, if any.

    This beats guessing from a product's model name, which only recognises the brands hard-coded
    above - a real Macron sports catalogue was branded "Premium Home" until this existed.
    """
    if not catalog_brand or not (catalog_brand.get("name") or "").strip():
        return None
    name = catalog_brand["name"].strip()
    primary = catalog_brand.get("primary_color") or DEFAULT_BRAND.primary_color
    return Brand(name=name, primary_color=primary, secondary_color=DEFAULT_BRAND.secondary_color, initial=name[0].upper())


def hero_image_for(product: Product) -> Optional[str]:
    """The product's own photo, a clearly-matching stock photo, or nothing at all.

    Nothing is the right answer when neither applies: the template then shows a branded panel
    instead of a stock image of some unrelated product."""
    if product.hero_image:
        return embed_local_image(product.hero_image)
    haystack = f"{product.category or ''} {product.model}".lower()
    for keywords, url in _STOCK_IMAGES:
        if any(k in haystack for k in keywords):
            return url
    return None


def embed_local_image(src: str) -> str:
    """Turn a "/storage/..." path into a base64 data URI. The PDF renderer opens the compiled
    HTML via file://, where "/storage/x.jpg" resolves against the filesystem root instead of
    this server, so local images would silently break in the PDF. External URLs pass through."""
    if not src.startswith("/storage/"):
        return src
    storage_root = settings.storage_dir.resolve()
    path = (storage_root / src[len("/storage/"):]).resolve()
    mime = _MIME_TYPES.get(path.suffix.lower())
    # Unsupported formats (e.g. .jp2) can't be displayed by the browser either way.
    if storage_root not in path.parents or mime is None or not path.is_file():
        return src
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _price(product: Product) -> Optional[str]:
    """Price with the catalog's own currency, or None when the catalog states no price
    (the template then shows "on request" rather than a made-up figure)."""
    if product.base_price <= 0:
        return None
    amount = f"{product.base_price:,.2f}"
    code = (product.currency or "USD").strip().upper()
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{symbol}{amount}" if symbol else f"{code} {amount}"


def compile_html(
    *,
    job_id: str,
    trace_id: str,
    segment: str,
    copy: dict,
    recommendations: List[Recommendation],
    products: List[Product],
    output_dir: Path,
    catalog_brand: Optional[dict] = None,
    blurbs: Optional[dict] = None,
    customer_name: Optional[str] = None,
) -> Path:
    catalog = brand_from_catalog(catalog_brand)
    by_id = {p.id: p for p in products}
    items = []
    for rec in recommendations:
        product = by_id[rec.product_id]
        items.append({
            "brand": catalog or brand_for_model(product.model),
            "product": {
                "model": product.model,
                "price": _price(product),
                "hero_image": hero_image_for(product),
                "category": product.category,
                "capacity": product.capacity,
                "power": product.power,
                "features": product.features,
            },
            "rec": rec,
            "blurb": (blurbs or {}).get(product.id),
        })

    html = _env.get_template("brochure.html").render(
        brand=items[0]["brand"],
        segment=segment,
        trace_id=trace_id,
        customer_name=(customer_name or "").strip() or "Valued Customer",
        items=items,
        copy={
            "headline": copy.get("headline", ""),
            "subheadline": copy.get("subheadline", ""),
            "paragraphs": (copy.get("paragraphs") or [])[:MAX_COVER_PARAGRAPHS],
            "cta": copy.get("cta", ""),
        },
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"compiled_{job_id}.html"
    out.write_text(html, encoding="utf-8")
    return out
