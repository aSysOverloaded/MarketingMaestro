"""Diagnose how well a catalog PDF will ingest, before spending embedding quota on it.

    python -m scripts.inspect_catalog "C:/path/to/catalog.pdf"

Reports, per page: extractable text length and image count, then a verdict on which
extraction strategy the catalog needs (plain text / layout-aware / OCR or vision), plus what
ingesting it will cost in embedding requests.
"""
import argparse
import statistics
from pathlib import Path

import pypdf

# A page with less than this much text has nothing useful for retrieval or extraction.
EMPTY_PAGE_CHARS = 50
# Rough marker for a page dense enough to hold several products.
DENSE_PAGE_CHARS = 1500


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf")
    parser.add_argument("--sample", type=int, default=3, help="pages to print a text sample from")
    parser.add_argument("--max-pages", type=int, default=0, help="only inspect the first N pages")
    args = parser.parse_args()

    path = Path(args.pdf)
    reader = pypdf.PdfReader(str(path))
    pages = reader.pages[: args.max_pages] if args.max_pages else reader.pages

    lengths, image_counts = [], []
    for page in pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        lengths.append(len(text))
        try:
            image_counts.append(len(page.images))
        except Exception:
            image_counts.append(0)

    total = len(lengths)
    empty = sum(1 for n in lengths if n < EMPTY_PAGE_CHARS)
    dense = sum(1 for n in lengths if n >= DENSE_PAGE_CHARS)
    median = int(statistics.median(lengths)) if lengths else 0

    print(f"file           {path.name}  ({path.stat().st_size / 1048576:.1f} MB)")
    print(f"pages          {total}")
    print(f"text per page  median {median} chars, max {max(lengths or [0])}")
    print(f"empty pages    {empty} ({empty / total:.0%}) - no extractable text")
    print(f"dense pages    {dense} ({dense / total:.0%}) - likely several products per page")
    print(f"images         {sum(image_counts)} total, median {int(statistics.median(image_counts or [0]))} per page")
    print()
    print(f"ingest cost    {total} embedding requests "
          f"(~{total / 100:.0f} min on a free tier limited to 100/minute)")
    print()

    print("verdict")
    if empty / total > 0.5:
        print("  - MOSTLY IMAGE PAGES: text extraction will not work. Needs OCR, or sending page")
        print("    images to a vision model. Without that, most of this catalog is invisible.")
    elif empty / total > 0.15:
        print(f"  - {empty} pages have no text and will be skipped silently. Worth an OCR/vision")
        print("    fallback for just those pages.")
    else:
        print("  - Text extraction works on nearly every page.")
    if dense / total > 0.3:
        print("  - Dense pages: one page = one chunk today, so several products on a page share")
        print("    one entry and one hero image. Worth splitting pages into product blocks.")
    if total > 200:
        print(f"  - Large catalog ({total} pages): ingest is slow on free embedding quota, and")
        print("    retrieval quality matters more because there is more to confuse it.")

    shown = [i for i, n in enumerate(lengths) if n >= EMPTY_PAGE_CHARS][: args.sample]
    for i in shown:
        text = (pages[i].extract_text() or "").strip()
        print(f"\n--- page {i + 1} sample ({lengths[i]} chars, {image_counts[i]} images) ---")
        print(text[:600])


if __name__ == "__main__":
    main()
