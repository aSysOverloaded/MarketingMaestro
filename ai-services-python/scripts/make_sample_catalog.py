"""Generate a multi-page sample catalog PDF, for benchmarking ingest/retrieval/extraction
without needing a real (possibly confidential) catalog.

    python -m scripts.make_sample_catalog --pages 120 --out storage/sample_catalog.pdf

Each page is one product with a name, price, specs and features, so extraction has something
real to find, and pages differ enough for retrieval to rank them.
"""
import argparse
import random
from pathlib import Path

CATEGORIES = [
    ("Trail Tent", "Tent", ["2-person", "3-person", "4-person"], ["Ripstop nylon fly", "Taped seams", "Colour-coded poles", "Vestibule storage"]),
    ("Camp Stove", "Stove", ["1-burner", "2-burner"], ["Piezo ignition", "Wind baffles", "Folding legs", "Simmer control"]),
    ("Sleeping Bag", "Sleeping bag", ["-5C comfort", "0C comfort", "5C comfort"], ["Mummy cut", "Draft collar", "Anti-snag zip", "Compression sack"]),
    ("Cook Set", "Cookware", ["2-piece", "4-piece", "6-piece"], ["Hard-anodised aluminium", "Folding handles", "Nesting design", "Mesh carry bag"]),
    ("Head Torch", "Lighting", ["220 lumen", "350 lumen", "500 lumen"], ["Red night mode", "USB-C charging", "IPX4 splashproof", "Adjustable band"]),
    ("Cool Box", "Storage", ["25 litre", "45 litre", "60 litre"], ["Rotomoulded shell", "Drain plug", "Bottle opener", "Non-slip feet"]),
    ("Hiking Backpack", "Backpack", ["30 litre", "45 litre", "65 litre"], ["Ventilated back panel", "Rain cover", "Hip belt pockets", "Ice axe loop"]),
    ("Camp Chair", "Furniture", ["Compact", "Standard", "Wide"], ["Powder-coated frame", "Cup holder", "Carry bag", "Mesh back"]),
]
BRANDS = ["Northpeak", "Vallis", "Kestrel", "Bramble", "Hollowbrook"]


def page_text(index: int, rng: random.Random) -> str:
    name, category, sizes, features = CATEGORIES[index % len(CATEGORIES)]
    brand = BRANDS[index % len(BRANDS)]
    size = sizes[index % len(sizes)]
    price = 39 + (index % 23) * 10
    chosen = rng.sample(features, 3)
    return (
        f"{brand} {name} {200 + index}    Page {index + 1}\\n"
        f"Category: {category}\\n"
        f"Size: {size}\\n"
        f"Price: ${price}.00\\n"
        f"Weight: {(index % 9) + 1}.{index % 10} kg\\n"
        f"Features: {'; '.join(chosen)}\\n"
        f"Ideal for weekend camping trips, festival use and back-garden overnighters. "
        f"Packs down small and sets up in minutes."
    )


def build_pdf(pages: list) -> bytes:
    objects = []
    contents_start = 3 + len(pages)  # 1 catalog, 2 pages tree, then page objects, then contents
    kids = " ".join(f"{3 + i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_obj = contents_start + len(pages)
    for i in range(len(pages)):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {contents_start + i} 0 R "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> >>".encode()
        )
    for text in pages:
        lines = text.split("\\n")
        body = "BT /F1 11 Tf 56 730 Td 16 TL\n" + "\n".join(f"({line.replace('(', '').replace(')', '')}) Tj T*" for line in lines) + "\nET"
        stream = body.encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=120)
    parser.add_argument("--out", default="storage/sample_catalog.pdf")
    args = parser.parse_args()

    rng = random.Random(7)
    pdf = build_pdf([page_text(i, rng) for i in range(args.pages)])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf)
    print(f"wrote {out} ({len(pdf) / 1024:.0f} KB, {args.pages} pages)")


if __name__ == "__main__":
    main()
