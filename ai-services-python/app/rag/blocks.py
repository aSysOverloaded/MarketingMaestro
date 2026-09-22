"""PDF -> product-sized blocks with their own images.

Why blocks rather than pages, and why the images are cropped from a page render, is in
docs/DECISIONS.md (D5, D6, D9). This module is all parsing and geometry: no embeddings, no
index, no LLM.
"""
import io
import logging
import os
import re
from typing import Optional

import pdfplumber
import pypdf

from app.observability import log_stage
from app.rag.layout import image_for_block, segment_words

logger = logging.getLogger("rag.blocks")


# Extracted page images are only ever used to pick one hero image per page, so keeping every
# image of a large catalog just fills the disk.
MAX_IMAGES_PER_PAGE = 2


# Blocks shorter than this are page furniture, stray codes or captions: not worth an index
# entry (and every entry costs an embedding request).
MIN_CHUNK_CHARS = 80


# Resolution for cropping a product photo out of the rendered page. JPEG, not PNG: 594 PNG
# crops of one catalogue came to 117 MB, and these are photographs.
IMAGE_RENDER_DPI = 110


IMAGE_JPEG_QUALITY = 82


# Smaller than this (in PDF points squared) is an icon or colour swatch, not a product shot.
MIN_PRODUCT_IMAGE_AREA = 2500


MIN_IMAGE_BYTES = 4096  # skip icons, logos, separators


# A line repeated on at least this share of pages is navigation/running header, not content.
BOILERPLATE_PAGE_SHARE = 0.4


MAX_BOILERPLATE_LINE_CHARS = 200


def find_boilerplate_lines(page_texts: list) -> set:
    """Lines that appear on a large share of pages: nav bars, running headers, page furniture.
    They add nothing to retrieval and dilute every page's embedding towards the same centre."""
    if len(page_texts) < 5:
        return set()
    counts = {}
    for text in page_texts:
        for line in {ln.strip() for ln in text.splitlines() if ln.strip()}:
            if len(line) <= MAX_BOILERPLATE_LINE_CHARS:
                counts[line] = counts.get(line, 0) + 1
    threshold = max(2, int(len(page_texts) * BOILERPLATE_PAGE_SHARE))
    return {line for line, n in counts.items() if n >= threshold}


def strip_boilerplate(text: str, boilerplate: set) -> str:
    return "\n".join(ln for ln in text.splitlines() if ln.strip() not in boilerplate).strip()


def select_page_images(page, page_number: int, dest_dir: str) -> list:
    """Save the largest MAX_IMAGES_PER_PAGE images of a page, biggest first.

    Every image is measured before any is written: a catalogue page can carry dozens of images
    (logos, colour swatches, icons), so taking the first ones that pass a size floor picks a
    banner rather than the product. images[0] becomes the brochure's hero image.
    """
    candidates = []
    for img_idx, img_file in enumerate(page.images):
        if len(img_file.data) < MIN_IMAGE_BYTES:
            continue  # icons/logos are never the hero image
        try:
            width, height = img_file.image.size
            area = width * height
        except Exception:
            area = len(img_file.data)  # undecodable: byte size is a reasonable proxy
        candidates.append((area, img_idx, img_file))

    candidates.sort(key=lambda c: c[0], reverse=True)
    paths = []
    for area, img_idx, img_file in candidates[:MAX_IMAGES_PER_PAGE]:
        ext = os.path.splitext(img_file.name)[1] if img_file.name else ".png"
        if not ext or ext == ".":
            ext = ".png"
        name = f"page_{page_number}_img_{img_idx}{ext}"
        with open(os.path.join(dest_dir, name), "wb") as f:
            f.write(img_file.data)
        paths.append(f"/storage/extracted_images/{name}")
    return paths


def _crop_block_image(plumber_page, block, page_number: int, block_index: int, dest_dir: str, rendered) -> Optional[str]:
    """Save this block's product photo, cropped out of the rendered page.

    Cropping the render (rather than pulling the embedded image stream) sidesteps exotic
    encodings a browser could not display anyway, and keeps the picture tied to where the
    product actually sits on the page.
    """
    image = image_for_block(plumber_page.images, block, min_area=MIN_PRODUCT_IMAGE_AREA)
    if image is None:
        return None

    scale = IMAGE_RENDER_DPI / 72
    box = (max(image["x0"] * scale, 0), max(image["top"] * scale, 0),
           min(image["x1"] * scale, rendered.width), min(image["bottom"] * scale, rendered.height))
    if box[2] - box[0] < 20 or box[3] - box[1] < 20:
        return None
    name = f"page_{page_number}_block_{block_index}.jpg"
    rendered.crop(box).convert("RGB").save(os.path.join(dest_dir, name), "JPEG", quality=IMAGE_JPEG_QUALITY)
    return f"/storage/extracted_images/{os.path.basename(dest_dir)}/{name}"


def chunk_pdf_by_layout(pdf_bytes: bytes, dest_dir: str, job_id: str) -> tuple:
    """Split every page into product-sized blocks with their own images (see app/rag/layout.py).

    Returns (chunks, stats). Falls back to one chunk per page - the previous behaviour - for
    any page pdfplumber cannot read.
    """
    chunks, stats = [], {"pages_with_text": 0, "image_failures": 0, "pages_fallback": 0}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_index, plumber_page in enumerate(pdf.pages):
            page_number = page_index + 1
            try:
                blocks = segment_words(plumber_page.extract_words())
            except Exception as e:
                log_stage(logger, job_id, "ingest", f"layout parsing failed on page {page_number}: {e}", level="warning")
                blocks = []
                stats["pages_fallback"] += 1

            blocks = merge_support_blocks(blocks)
            kept = [b for b in blocks if len(b.text) >= MIN_CHUNK_CHARS]
            if not kept:
                continue
            stats["pages_with_text"] += 1

            rendered = None
            for block_index, block in enumerate(kept):
                image_path = None
                try:
                    if plumber_page.images:
                        if rendered is None:
                            rendered = plumber_page.to_image(resolution=IMAGE_RENDER_DPI).original
                        image_path = _crop_block_image(plumber_page, block, page_number, block_index, dest_dir, rendered)
                except Exception as e:
                    stats["image_failures"] += 1
                    log_stage(logger, job_id, "ingest", f"image crop failed on page {page_number}: {e}", level="warning")

                chunks.append({
                    "chunk_id": f"p{page_number}b{block_index}",
                    "page_number": page_number,
                    "block_index": block_index,
                    "content": block.text,
                    "images": [image_path] if image_path else [],
                })
            plumber_page.close()  # pdfplumber caches per-page objects; large catalogs need this
    return chunks, stats


def chunk_pdf_by_page(pdf_bytes: bytes, dest_dir: str, job_id: str) -> tuple:
    """Fallback chunking: one chunk per page, images picked by size (pypdf)."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    chunks, stats = [], {"pages_with_text": 0, "image_failures": 0, "pages_fallback": len(reader.pages)}
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        stats["pages_with_text"] += 1
        try:
            images = select_page_images(page, i + 1, dest_dir)
        except Exception as e:
            images = []
            stats["image_failures"] += 1
            log_stage(logger, job_id, "ingest", f"failed to extract images on page {i+1}: {e}", level="warning")
        chunks.append({"chunk_id": f"p{i + 1}b0", "page_number": i + 1, "block_index": 0,
                       "content": text, "images": images})
    return chunks, stats


# Catalogs carry front matter: covers, index pages, brand stories, campus photos. They are not
# products, but they cost an embedding request each and compete in search (a run actually
# matched the cover page). A block is taken to be a product when it states a price or a long
# product code.
_PRICE = re.compile(r"(?:UVP|EUR|USD|GBP|INR|[€$£₹])\s?\d+[.,]?\d*", re.IGNORECASE)


_PRODUCT_CODE = re.compile(r"(?<!\d)\d{6,9}(?!\d)")


# Below this share, the catalog simply does not print prices or codes, and filtering would
# throw the whole catalog away - so everything is kept.
MIN_PRODUCT_BLOCK_SHARE = 0.3


def looks_like_product(text: str) -> bool:
    return bool(_PRICE.search(text) or _PRODUCT_CODE.search(text))


# A support block (colour swatch row, size run) states no price or code of its own but belongs
# to the product beside it. Merging it in gives the extractor the colours it would otherwise
# never see - the real catalogue lists them in separate blocks - and saves an embedding request.
MAX_SUPPORT_BLOCK_CHARS = 500


MAX_SUPPORT_DISTANCE = 400.0


def merge_support_blocks(blocks: list) -> list:
    """Fold colour/size blocks into the nearest product block on the same page."""
    products = [b for b in blocks if looks_like_product(b.text)]
    if not products:
        return blocks

    extra_text: dict = {}
    merged_away = set()
    for index, block in enumerate(blocks):
        if block in products or len(block.text) > MAX_SUPPORT_BLOCK_CHARS:
            continue
        centre_x, centre_y = (block.x0 + block.x1) / 2, (block.top + block.bottom) / 2
        nearest = min(products, key=lambda p: p.distance_to(centre_x, centre_y))
        if nearest.distance_to(centre_x, centre_y) > MAX_SUPPORT_DISTANCE:
            continue
        extra_text.setdefault(id(nearest), []).append(block.text)
        merged_away.add(index)

    result = []
    for index, block in enumerate(blocks):
        if index in merged_away:
            continue
        if id(block) in extra_text:
            block.text = block.text + "\n" + "\n".join(extra_text[id(block)])
        result.append(block)
    return result


def filter_product_blocks(chunks: list, job_id: str = "unknown") -> list:
    """Keep only product blocks - unless too few blocks look like products to judge."""
    product_chunks = [c for c in chunks if looks_like_product(c["content"])]
    if not chunks or len(product_chunks) / len(chunks) < MIN_PRODUCT_BLOCK_SHARE:
        return chunks
    log_stage(logger, job_id, "ingest",
              f"keeping {len(product_chunks)} product block(s), dropping {len(chunks) - len(product_chunks)} "
              f"non-product block(s) (covers, index, brand pages)")
    return product_chunks
