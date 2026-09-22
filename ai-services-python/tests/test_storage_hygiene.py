"""Generated files must not grow without bound, and a replaced catalog's images must go.

One catalogue produced 594 crops (117 MB as PNG) and 18 brochures (29 MB) with nothing ever
deleting them.
"""
from app.config import settings
from app.pipeline.brochure import KEEP_RECENT_OUTPUTS, prune_old_outputs
from app.rag import index
from app.rag import search


def _make(folder, name, mtime):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x")
    import os
    os.utime(path, (mtime, mtime))
    return path


def test_only_the_most_recent_outputs_are_kept(isolated_storage):
    pdfs = isolated_storage / "generated_brochures"
    htmls = isolated_storage / "temp_brochures"
    for i in range(KEEP_RECENT_OUTPUTS + 5):
        _make(pdfs, f"brochure_{i}.pdf", 1000 + i)
        _make(htmls, f"compiled_{i}.html", 1000 + i)

    prune_old_outputs()

    remaining = sorted(p.name for p in pdfs.glob("*.pdf"))
    assert len(remaining) == KEEP_RECENT_OUTPUTS
    assert "brochure_24.pdf" in remaining and "brochure_0.pdf" not in remaining  # newest kept, oldest gone
    assert len(list(htmls.glob("*.html"))) == KEEP_RECENT_OUTPUTS


def test_pruning_is_safe_when_there_is_nothing_to_prune(isolated_storage):
    prune_old_outputs()  # no folders yet
    _make(isolated_storage / "generated_brochures", "only.pdf", 1000)
    prune_old_outputs()
    assert (isolated_storage / "generated_brochures" / "only.pdf").exists()


def test_replacing_a_catalog_drops_the_old_catalogs_images(isolated_storage):
    root = isolated_storage / "extracted_images"
    _make(root / "oldcatalog0123456", "page_1_block_0.jpg", 1000)
    _make(root / "newcatalog9876543", "page_1_block_0.jpg", 2000)
    _make(root, "page_1_img_0.png", 1000)  # flat file from an older version

    index._drop_other_image_folders(keep="newcatalog9876543")

    assert (root / "newcatalog9876543" / "page_1_block_0.jpg").exists()
    assert not (root / "oldcatalog0123456").exists()
    assert not (root / "page_1_img_0.png").exists()
