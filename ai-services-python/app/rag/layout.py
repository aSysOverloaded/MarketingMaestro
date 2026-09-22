"""Layout-aware page segmentation: turn a PDF page into product-sized blocks.

Why this exists, measured on a real 133-page sports catalogue:

- 71% of its pages hold several products. Indexing one page as one chunk meant several
  products shared a single index entry and a single hero image, and the extractor received a
  blob it had to guess its way through.
- Reading text in stream order merges adjacent columns, e.g.
  `SOFTLOCK + MESH: 100% POLYESTERAvailable until 202880000274` - three columns run together.

So text is read with word coordinates (pdfplumber) and split the way the page looks: first at
vertical gutters (whitespace columns running down the page), then at vertical gaps inside each
column. Images are attached to the block they sit in, instead of "biggest image on the page".

This module is pure geometry - no I/O, no embedding - so it is testable with plain word dicts.
"""
from typing import Dict, List, Optional, Sequence

# A run of empty x-bins this wide is a gutter between columns.
BIN_WIDTH = 5.0
MIN_GUTTER_WIDTH = 18.0
# A vertical gap starts a new block when it exceeds both of these: a multiple of the typical
# line height, and an absolute floor. Without the floor, a product's name, size row and
# feature list - separated by ordinary paragraph spacing - each became their own block.
LINE_GAP_FACTOR = 2.2
MIN_BLOCK_GAP = 45.0
# Blocks shorter than this are page furniture or strays, and get merged into the block above.
MIN_BLOCK_CHARS = 40


class Block:
    """A product-sized region of a page: its text and where it sits."""

    def __init__(self, words: Sequence[dict]):
        self.words = list(words)
        self.x0 = min(w["x0"] for w in self.words)
        self.x1 = max(w["x1"] for w in self.words)
        self.top = min(w["top"] for w in self.words)
        self.bottom = max(w["bottom"] for w in self.words)
        self.text = _words_to_text(self.words)

    def contains(self, x: float, y: float, margin: float = 12.0) -> bool:
        return (self.x0 - margin) <= x <= (self.x1 + margin) and (self.top - margin) <= y <= (self.bottom + margin)

    def distance_to(self, x: float, y: float) -> float:
        cx, cy = (self.x0 + self.x1) / 2, (self.top + self.bottom) / 2
        return ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5


def _words_to_text(words: Sequence[dict]) -> str:
    """Join words into lines, preserving reading order within the block."""
    if not words:
        return ""
    heights = sorted(w["bottom"] - w["top"] for w in words)
    tolerance = max(heights[len(heights) // 2] * 0.6, 2.0)

    lines: List[List[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return "\n".join(" ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])) for line in lines).strip()


def _split_at_gutters(words: Sequence[dict]) -> List[List[dict]]:
    """Split words into columns at vertical whitespace running down the page."""
    if not words:
        return []
    left = min(w["x0"] for w in words)
    right = max(w["x1"] for w in words)
    bins = int((right - left) / BIN_WIDTH) + 1
    occupied = [False] * bins
    for w in words:
        for b in range(int((w["x0"] - left) / BIN_WIDTH), int((w["x1"] - left) / BIN_WIDTH) + 1):
            if 0 <= b < bins:
                occupied[b] = True

    # Gutter = a long enough run of empty bins that is not at either edge.
    boundaries, run_start = [], None
    for i, filled in enumerate(occupied + [True]):
        if not filled:
            run_start = i if run_start is None else run_start
            continue
        if run_start is not None:
            if (i - run_start) * BIN_WIDTH >= MIN_GUTTER_WIDTH and run_start > 0:
                boundaries.append(left + (run_start + (i - run_start) / 2) * BIN_WIDTH)
            run_start = None

    if not boundaries:
        return [list(words)]

    columns: List[List[dict]] = [[] for _ in range(len(boundaries) + 1)]
    for w in words:
        centre = (w["x0"] + w["x1"]) / 2
        index = sum(1 for b in boundaries if centre > b)
        columns[index].append(w)
    return [c for c in columns if c]


def _split_at_vertical_gaps(words: Sequence[dict]) -> List[List[dict]]:
    """Within one column, start a new block wherever the vertical spacing jumps."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    heights = sorted(w["bottom"] - w["top"] for w in ordered)
    line_height = heights[len(heights) // 2]
    gap_limit = max(line_height * LINE_GAP_FACTOR, MIN_BLOCK_GAP)

    groups: List[List[dict]] = [[ordered[0]]]
    last_bottom = ordered[0]["bottom"]
    for word in ordered[1:]:
        if word["top"] - last_bottom > gap_limit:
            groups.append([])
        groups[-1].append(word)
        last_bottom = max(last_bottom, word["bottom"])
    return groups


def drop_rotated(words: Sequence[dict]) -> List[dict]:
    """Remove sideways text. Catalogue pages carry rotated nav tabs down the edge, which
    pdfplumber reads character-by-character in visual order ("BASEBALL" -> "LLABESAB")."""
    return [w for w in words if w.get("upright", True)]


def segment_words(words: Sequence[dict]) -> List[Block]:
    """Split a page's words into blocks: columns first, then vertical gaps within each column."""
    words = drop_rotated(words)
    blocks: List[Block] = []
    for column in _split_at_gutters(words):
        for group in _split_at_vertical_gaps(column):
            if group:
                blocks.append(Block(group))

    # Reading order: top-to-bottom within a column, columns left-to-right.
    blocks.sort(key=lambda b: (round(b.x0 / 50), b.top))

    merged: List[Block] = []
    for block in blocks:
        # A stray line (a heading on its own, a code) belongs with the block above it.
        if merged and len(block.text) < MIN_BLOCK_CHARS:
            merged[-1] = Block(merged[-1].words + block.words)
        else:
            merged.append(block)
    return merged


def image_for_block(images: Sequence[Dict], block: Block, min_area: float = 0.0) -> Optional[Dict]:
    """The image belonging to a block, or None.

    A catalogue sets the photo *beside* its text, not inside it: on the real catalogue's page 31
    the balls sit at x -14..170 while their text blocks start at x 175. So the image whose
    vertical span overlaps the block most wins, and ties go to the nearest horizontally.
    """
    def vertical_overlap(image) -> float:
        return max(0.0, min(block.bottom, image["bottom"]) - max(block.top, image["top"]))

    def horizontal_distance(image) -> float:
        return abs((image["x0"] + image["x1"]) / 2 - (block.x0 + block.x1) / 2)

    candidates = [im for im in images if area_of(im) >= min_area and vertical_overlap(im) > 0]
    if not candidates:
        return None
    block_height = max(block.bottom - block.top, 1)
    return max(candidates, key=lambda im: (round(vertical_overlap(im) / block_height, 1), -horizontal_distance(im)))


def area_of(image: Dict) -> float:
    return (image["x1"] - image["x0"]) * (image["bottom"] - image["top"])
