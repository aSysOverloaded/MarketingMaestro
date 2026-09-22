"""Ingest-time text and image quality, driven by what a real 133-page sports catalogue does:
every page repeats a navigation bar, and pages carry up to 44 images each."""
from types import SimpleNamespace

from app.rag import blocks
from app.rag import search

NAV = "ACCESSORIES JACKETS ATHLEISURE VOLLEYBALL"


def _pages(n=10):
    return [f"{NAV}\nProduct {i} details\nUnique line {i}" for i in range(n)]


def test_repeated_page_furniture_is_detected_and_stripped():
    boilerplate = blocks.find_boilerplate_lines(_pages())
    assert NAV in boilerplate
    assert "Product 0 details" not in boilerplate  # per-page content survives

    cleaned = blocks.strip_boilerplate(_pages()[0], boilerplate)
    assert cleaned.splitlines()[0] == "Product 0 details"


def test_boilerplate_needs_a_real_repeat_and_short_documents_are_left_alone():
    assert blocks.find_boilerplate_lines(["a", "b"]) == set()  # too few pages to judge
    pages = _pages(10)
    pages[0] += "\nAppears twice only"
    pages[1] += "\nAppears twice only"
    assert "Appears twice only" not in blocks.find_boilerplate_lines(pages)


class _Img:
    def __init__(self, name, size, data_len):
        self.name = name
        self.data = b"x" * data_len
        self.image = SimpleNamespace(size=size)


def test_hero_image_is_the_largest_on_the_page_not_the_first(tmp_path):
    page = SimpleNamespace(images=[
        _Img("logo.png", (80, 80), 9000),          # first, but tiny
        _Img("swatch.png", (40, 40), 500),         # below the byte floor
        _Img("product.jpg", (1600, 1200), 90000),  # the real product shot
        _Img("banner.png", (900, 300), 40000),
    ])
    paths = blocks.select_page_images(page, 54, str(tmp_path))

    assert len(paths) == blocks.MAX_IMAGES_PER_PAGE
    assert paths[0].endswith("page_54_img_2.jpg")  # product.jpg, the largest by area
    assert not any("img_1" in p for p in paths)    # the sub-4KB swatch is skipped
    assert len(list(tmp_path.iterdir())) == blocks.MAX_IMAGES_PER_PAGE  # only winners written


def test_undecodable_images_fall_back_to_byte_size(tmp_path):
    broken = _Img("broken.png", (10, 10), 200000)
    broken.image = None  # .size raises
    page = SimpleNamespace(images=[broken, _Img("small.png", (50, 50), 5000)])
    paths = blocks.select_page_images(page, 1, str(tmp_path))
    assert paths[0].endswith("page_1_img_0.png")
