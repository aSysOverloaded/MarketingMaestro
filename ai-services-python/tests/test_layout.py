"""Page segmentation: pure geometry, tested with word dicts shaped like pdfplumber's.

Modelled on a real catalogue page: two columns, each holding a product whose name, size row
and feature list are separated by ordinary paragraph spacing, plus rotated nav text.
"""
from app.rag.layout import Block, assign_image_to_block, drop_rotated, segment_words


def word(text, x0, top, width=40, height=8):
    return {"text": text, "x0": x0, "x1": x0 + width, "top": top, "bottom": top + height, "upright": True}


def product(prefix, x, y):
    return [
        word(f"{prefix}CODE", x, y), word(f"{prefix}NAME", x + 45, y),
        word(f"{prefix}FABRIC", x, y + 14),          # 6pt gap: same block
        word(f"{prefix}SIZES", x, y + 34),           # 12pt gap: still the same product
        word(f"{prefix}FEATUREONE", x, y + 50), word(f"{prefix}FEATURETWO", x + 45, y + 50),
    ]


def test_columns_are_split_at_the_gutter():
    words = product("LEFT", 50, 100) + product("RIGHT", 400, 100)
    blocks = segment_words(words)

    assert len(blocks) == 2
    assert all("LEFT" in blocks[0].text and "RIGHT" not in blocks[0].text for _ in [0])
    assert "RIGHT" in blocks[1].text
    # the flattening this replaces would have merged the two columns line by line
    assert "LEFTCODE LEFTNAME" in blocks[0].text


def test_one_product_stays_one_block_but_a_distant_product_does_not():
    words = product("TOP", 50, 100) + product("BOTTOM", 50, 400)
    blocks = segment_words(words)

    assert len(blocks) == 2
    assert "TOPFEATUREONE" in blocks[0].text and "TOPSIZES" in blocks[0].text
    assert "BOTTOM" not in blocks[0].text


def test_rotated_navigation_text_is_dropped():
    sideways = {"text": "LLABESAB", "x0": 5, "x1": 15, "top": 10, "bottom": 300, "upright": False}
    assert drop_rotated([sideways]) == []
    blocks = segment_words(product("P", 50, 100) + [sideways])
    assert all("LLABESAB" not in b.text for b in blocks)


def test_stray_short_lines_join_the_block_above():
    words = product("P", 50, 100) + [word("99", 50, 300)]  # lone code far below
    blocks = segment_words(words)
    assert len(blocks) == 1 and blocks[0].text.endswith("99")


def test_images_belong_to_the_block_they_sit_in():
    blocks = segment_words(product("LEFT", 50, 100) + product("RIGHT", 400, 100))
    inside_left = {"x0": 55, "x1": 95, "top": 105, "bottom": 145}
    below_right = {"x0": 405, "x1": 445, "top": 900, "bottom": 940}

    assert assign_image_to_block(inside_left, blocks) == 0
    assert assign_image_to_block(below_right, blocks) == 1  # nearest when inside nothing
    assert assign_image_to_block(inside_left, []) is None


def test_block_geometry():
    block = Block([word("a", 10, 20), word("b", 100, 60)])
    assert (block.x0, block.top, block.x1, block.bottom) == (10, 20, 140, 68)
    assert block.contains(50, 40) and not block.contains(500, 40)
