"""Figures of a source PDF: found by their captions, cropped and saved as PNG images.

A study page shows a paper's own figures beside the prose that explains them. Which figures a
PDF has, where they are and what their crops look like is decided here, deterministically and
locally (with `pypdfium2`, which renders pages and exposes text positions); the writer model is
only told which figures exist and may place those, by label (see the `figures` stage of
`teacher.claude_generator.ClaudeGenerator`).

Detection, per page (coordinates in PDF points, origin at the top left of the page's crop box):

1. Text lines: the text page's rectangles, merged into lines (pieces on one baseline with a gap
   of at most about one line height; a column gap is wider and keeps columns apart).
2. Captions: a line that starts with "Figure N" or "Fig. N" followed by ":", ".", "|", nothing,
   or a capital letter (a separator drawn apart from the text, as in "Figure 2 | The ...")
   (N is a number, optionally after one capital letter, as in "Figure A1"). A line such as
   "Figure 3 shows ..." or "Figure 3(a) ..." is a reference in the body, not a caption; a body
   line that happens to look like one finds no graphic next to it and is dropped. The caption
   runs on over the lines right below it (same column, line spacing) until a gap, another
   caption or a line that starts outside the caption's left edge. Its text (without the label
   and separator) is the figure's source caption.
3. Graphics: the page is rendered a second time with every text object removed (also inside
   form XObjects), at one pixel per point; every pixel that is not near-white there is "ink",
   i.e. part of an image or a vector drawing. Rendering, rather than reading object bounds,
   respects clipping and form transformations. An image placed on the page counts as ink over
   its whole rectangle, so a screenshot's white background belongs to it.
4. The column: on a page whose body text runs in two columns (lines wholly in each half), a
   caption wholly within one half belongs to that column; otherwise it spans the page. Captions
   side by side at the same height split the width between them.
5. The region: in the caption's column, the slot between the caption and the nearest obstacle
   above it (a body text line, another caption or a table caption, the page's top margin),
   holds the figure. A body text line is one of running-text size, at least half the column
   wide, not drawn on a graphic and not framed by one (ink to its left and right, as in a box
   of text inside a figure); the short last line of a paragraph, right below a body line and
   starting where it starts, is one too. Starting at the caption and moving away from it, runs of ink rows and text
   lines join the region while the gap to it stays small (`JOIN_GAP`); the first one must lie
   within `FIRST_GAP` of the caption. Text lines beside the region (axis labels, legends) join
   it too. If nothing with ink lies above the caption, the same is tried below it (a caption
   above its figure). The caption itself is never part of the crop.
6. Sanity checks drop a candidate, with the reason recorded: no graphic next to the caption,
   a region too small (`MIN_SIDE` points), one covering nearly the whole page
   (`MAX_PAGE_FRACTION` of the page inside its top and bottom margins), mostly text (little
   ink, and text that is neither drawn on a graphic nor framed by one covering much of it), a
   rotated page. Only the first caption of each label that yields a good region is kept; a
   label whose every candidate fails is recorded as dropped with each reason. Fewer correct
   figures are preferred to many doubtful ones.

Rendering: each kept region (plus a small margin that never reaches an obstacle) is rendered
at `RENDER_SCALE` (144 dpi, so a figure across a text column is sharp on a high-density
screen; at most `MAX_PIXELS` on its longer side), near-white borders are trimmed, and the image
is written as an 8-bit RGB PNG (encoded here with zlib, so no imaging library is needed).

The stage (`find_source_figures`, run by the generator, which saves `FiguresResult.record`)
records what it found in `<artifact>/generation/figures.json`
(each figure's label, page, caption text, region and pixel size, and every dropped candidate
with its reason) and writes the crops to `<artifact>/assets/figures/`, named after the label
and a digest of the image, so a file name never refers to two different images. The folder is
emptied first: its files belong to this stage alone, and a page only ever names crops of the
run that wrote it. (Regenerating a page creates a new artifact with a folder of its own, so the
old page keeps its crops and the new page cannot see them.)

A source that is not a PDF (a web page saved as text, which has no pages) gets no figures, and
neither does a PDF in which nothing is detected: the record says so plainly and the writer is
told that no figures are available (`figures_prompt`, the write prompt's list of figures).
That is a result, not a fallback. A PDF that cannot be opened or rendered raises
`FigureError`, which fails the stage.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from teacher.locators import PdfRegion

FIGURES_STAGE = "figures"
FIGURES_FILE = "figures.json"
ASSETS_DIR = "assets"
FIGURES_DIR = "figures"  # under the artifact's assets/

# -- detection parameters (PDF points unless said otherwise) --------------------------------
INK_THRESHOLD = 235  # a mask pixel is ink when one of its channels is darker than this
MASK_SCALE = 1.0  # pixels per point of the graphics mask
FIRST_GAP = 28.0  # the region's nearest part lies at most this far from the caption
JOIN_GAP = 22.0  # a further part joins the region when at most this far from it
SIDE_GAP = 14.0  # a text line beside the region (axis label, legend) joins when this close
MARGIN = 0.06  # top and bottom fraction of the page that is header or footer, never figure
MIN_SIDE = 40.0  # a region narrower or lower than this is too small to be a figure
MIN_AREA_FRACTION = 0.012  # ... and so is one smaller than this fraction of the page
MAX_PAGE_FRACTION = 0.85  # a region over this fraction of the page within its margins: no figure
BODY_LINE_FRACTION = 0.5  # a line at least this fraction of the column wide may be body text
TEXT_COVER_LIMIT = 0.4  # loose text covering more than this fraction: mostly text ...
TEXT_INK_LIMIT = 0.04  # ... when the ink covers less than this fraction of the region
PAD = 4.0  # margin rendered around a region, unless an obstacle is closer
RENDER_SCALE = 2.0  # pixels per point of a crop (144 dpi)
MAX_PIXELS = 2000  # longest side of a crop
TRIM_LEVEL = 245  # near-white rows and columns at a crop's border are trimmed
TRIM_KEEP = 6  # pixels of white kept around the trimmed content

# "Figure 3:", "Fig. 3.", "Figure 3 |", "Figure 3" alone on its line, or "Figure 3 The ..." (a
# separator drawn apart from the text); not "Figure 3 shows ..." or "Figure 3(a) ...".
CAPTION_RE = re.compile(r"^(?:Figure|Fig\.)\s*([A-Z]?\d+)(?:\s*[:.|]|\s*$|\s+(?=[A-Z]))")
OTHER_CAPTION_RE = re.compile(
    r"^(?:Table|Tab\.|Algorithm|Listing)\s*[A-Z]?\d+(?:\s*[:.|]|\s*$|\s+(?=[A-Z]))"
)


class FigureError(Exception):
    """The PDF's figures could not be detected or rendered (an unreadable or broken file)."""


# -- geometry -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Box:
    """A rectangle in points, origin top left: x0 < x1 left to right, y0 < y1 top to bottom."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def union(self, other: Box) -> Box:
        return Box(
            min(self.x0, other.x0), min(self.y0, other.y0),
            max(self.x1, other.x1), max(self.y1, other.y1),
        )

    def x_overlap(self, other: Box) -> float:
        return min(self.x1, other.x1) - max(self.x0, other.x0)

    def y_overlap(self, other: Box) -> float:
        return min(self.y1, other.y1) - max(self.y0, other.y0)

    def x_gap(self, other: Box) -> float:
        return max(0.0, max(self.x0, other.x0) - min(self.x1, other.x1))

    def y_gap(self, other: Box) -> float:
        return max(0.0, max(self.y0, other.y0) - min(self.y1, other.y1))

    def clip(self, other: Box) -> Box:
        return Box(
            max(self.x0, other.x0), max(self.y0, other.y0),
            min(self.x1, other.x1), min(self.y1, other.y1),
        )


def _union(boxes: Iterable[Box]) -> Box | None:
    result: Box | None = None
    for box in boxes:
        result = box if result is None else result.union(box)
    return result


@dataclass(frozen=True)
class Line:
    """A line of text on the page: its box and its text."""

    box: Box
    text: str


# -- results --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Figure:
    """A detected figure: `label` ("Figure 3"), the `page` it is on (from 1), the source's own
    `caption` text, its `region` on the page and, once rendered, the crop's `asset` path
    (relative to the artifact's `assets/` folder) and size in pixels."""

    label: str
    page: int
    caption: str
    region: PdfRegion
    box: Box  # the region in points
    asset: str = ""
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "page": self.page,
            "caption": self.caption,
            "region": self.region.model_dump(),
            "asset": self.asset,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class Dropped:
    """A caption whose figure was not kept, and why."""

    label: str
    page: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "page": self.page, "reason": self.reason}


@dataclass(frozen=True)
class FigureScan:
    """What detection found in one PDF: the kept `figures` in page order and the `dropped`
    candidates (a label whose every candidate failed, with each candidate's reason)."""

    pages: int
    figures: tuple[Figure, ...]
    dropped: tuple[Dropped, ...] = ()

    def by_label(self) -> dict[str, Figure]:
        return {figure.label: figure for figure in self.figures}


# -- reading a page -------------------------------------------------------------------------


@dataclass
class _Page:
    number: int  # from 1
    width: float
    height: float
    lines: list[Line]
    ink: np.ndarray | None = None  # bool [rows, cols] at MASK_SCALE; set for caption pages
    two_columns: bool = False
    captions: list[_Caption] = field(default_factory=list)


@dataclass(frozen=True)
class _Caption:
    label: str
    first: Line
    box: Box
    text: str


def _text_lines(textpage: Any, width: float, height: float, left: float, bottom: float) -> list[Line]:
    """The page's text as lines (rectangles on one baseline joined), top to bottom."""
    pieces: list[Line] = []
    for index in range(textpage.count_rects()):
        l, b, r, t = textpage.get_rect(index)
        text = textpage.get_text_bounded(l, b, r, t)
        text = " ".join(text.replace("\x02", "").replace("­", "").split())
        if not text:
            continue
        box = Box(l - left, height - (t - bottom), r - left, height - (b - bottom))
        pieces.append(Line(box, text))
    # Rows: pieces that overlap vertically by at least half the smaller height.
    pieces.sort(key=lambda line: line.box.y0)
    rows: list[list[Line]] = []
    for piece in pieces:
        for row in reversed(rows[-8:]):
            anchor = row[0].box
            smaller = min(anchor.height, piece.box.height)
            if smaller > 0 and anchor.y_overlap(piece.box) >= 0.5 * smaller:
                row.append(piece)
                break
        else:
            rows.append([piece])
    # Within a row, pieces closer than a line height (or overlapping) form one line.
    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda line: line.box.x0)
        current = row[0]
        for piece in row[1:]:
            gap = piece.box.x0 - current.box.x1
            if gap <= max(current.box.height, piece.box.height):
                inside = piece.box.x1 <= current.box.x1 and not piece.text.strip("|:.")
                text = current.text if inside else f"{current.text} {piece.text}"
                current = Line(current.box.union(piece.box), text)
            else:
                lines.append(current)
                current = piece
        lines.append(current)
    lines.sort(key=lambda line: (line.box.y0, line.box.x0))
    return lines


def _strip_text(container: Any, *, form: bool) -> None:
    """Remove every text object from a page (or a form XObject), recursing into forms."""
    count = (
        pdfium_c.FPDFFormObj_CountObjects(container)
        if form
        else pdfium_c.FPDFPage_CountObjects(container)
    )
    for index in range(count - 1, -1, -1):
        obj = (
            pdfium_c.FPDFFormObj_GetObject(container, index)
            if form
            else pdfium_c.FPDFPage_GetObject(container, index)
        )
        kind = pdfium_c.FPDFPageObj_GetType(obj)
        if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
            removed = (
                pdfium_c.FPDFFormObj_RemoveObject(container, obj)
                if form
                else pdfium_c.FPDFPage_RemoveObject(container, obj)
            )
            if removed:
                pdfium_c.FPDFPageObj_Destroy(obj)
        elif kind == pdfium_c.FPDF_PAGEOBJ_FORM:
            _strip_text(obj, form=True)


def _ink_mask(page: Any, width: float, height: float, left: float, bottom: float) -> np.ndarray:
    """Where the text-free page has ink, as a bool array at `MASK_SCALE`. An image placed on the
    page itself counts as ink over its whole rectangle, white parts included: a screenshot's
    white background is still part of the figure."""
    images = [
        obj.get_bounds()
        for obj in page.get_objects(max_depth=0)
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE
    ]
    _strip_text(page.raw, form=False)
    bitmap = page.render(scale=MASK_SCALE, rev_byteorder=True, draw_annots=False)
    pixels = bitmap.to_numpy()
    ink = (pixels[:, :, :3] < INK_THRESHOLD).any(axis=2)
    rows, cols = ink.shape
    for l, b, r, t in images:
        x0 = max(0, int((l - left) * MASK_SCALE))
        x1 = min(cols, int(np.ceil((r - left) * MASK_SCALE)))
        y0 = max(0, int((height - (t - bottom)) * MASK_SCALE))
        y1 = min(rows, int(np.ceil((height - (b - bottom)) * MASK_SCALE)))
        if x1 > x0 and y1 > y0 and (x1 - x0) * (y1 - y0) < MAX_PAGE_FRACTION * rows * cols:
            ink[y0:y1, x0:x1] = True
    return ink


def _two_columns(lines: Sequence[Line], width: float) -> bool:
    """Whether the page's body text runs in two columns: many wide lines in each half."""
    middle = width / 2
    left = sum(1 for l in lines if l.box.x1 <= middle + 2 and l.box.width >= 0.25 * width)
    right = sum(1 for l in lines if l.box.x0 >= middle - 2 and l.box.width >= 0.25 * width)
    return left >= 5 and right >= 5


def _captions(lines: Sequence[Line]) -> list[_Caption]:
    """Every caption start and its continuation lines."""
    found: list[_Caption] = []
    for index, line in enumerate(lines):
        match = CAPTION_RE.match(line.text)
        if match is None:
            continue
        label = f"Figure {match.group(1)}"
        box, texts = line.box, [line.text]
        last = line
        for following in lines[index + 1:]:
            if following.box.y0 < last.box.y1 - 0.5 * last.box.height:
                continue  # on the same line (beside the caption, in another column)
            gap = following.box.y0 - last.box.y1
            if gap > 0.7 * last.box.height:
                break
            if following.box.x0 < box.x0 - 2 * last.box.height or following.box.x0 > box.x1:
                continue  # another column
            if following.box.x1 > box.x1 + 4 * last.box.height and box.width > 0:
                # wider than the caption by far: body text of a full-width layout
                if following.box.width > 1.5 * box.width:
                    break
            if CAPTION_RE.match(following.text) or OTHER_CAPTION_RE.match(following.text):
                break
            box = box.union(following.box)
            texts.append(following.text)
            last = following
        text = _join(texts)[match.end():].lstrip(" :.|")
        found.append(_Caption(label, line, box, text))
    return found


def _join(texts: Sequence[str]) -> str:
    out = ""
    for text in texts:
        if not out:
            out = text
        elif out.endswith("-"):
            out += text
        else:
            out += " " + text
    return out


# -- finding the region ---------------------------------------------------------------------


def _column(caption: _Caption, page: _Page) -> tuple[float, float]:
    """The horizontal extent the caption's figure may occupy."""
    x0, x1 = 0.0, page.width
    middle = page.width / 2
    if page.two_columns:
        if caption.box.x1 <= middle + 0.03 * page.width:
            x1 = middle
        elif caption.box.x0 >= middle - 0.03 * page.width:
            x0 = middle
    for other in page.captions:
        if other is caption or other.first.box.y_overlap(caption.first.box) <= 0:
            continue
        if other.box.x1 <= caption.box.x0:
            x0 = max(x0, (other.box.x1 + caption.box.x0) / 2)
        elif other.box.x0 >= caption.box.x1:
            x1 = min(x1, (caption.box.x1 + other.box.x0) / 2)
    return x0, x1


def _ink_in(page: _Page, box: Box) -> np.ndarray:
    assert page.ink is not None
    rows, cols = page.ink.shape
    y0 = max(0, int(box.y0 * MASK_SCALE))
    y1 = min(rows, int(np.ceil(box.y1 * MASK_SCALE)))
    x0 = max(0, int(box.x0 * MASK_SCALE))
    x1 = min(cols, int(np.ceil(box.x1 * MASK_SCALE)))
    if y1 <= y0 or x1 <= x0:
        return np.zeros((0, 0), dtype=bool)
    return page.ink[y0:y1, x0:x1]


def _ink_fraction(page: _Page, box: Box) -> float:
    part = _ink_in(page, box)
    return float(part.mean()) if part.size else 0.0


def _is_body_line(line: Line, page: _Page, column: tuple[float, float], body_height: float) -> bool:
    """A line of running text: wide for its column, of body size, not drawn on a graphic."""
    width = column[1] - column[0]
    if line.box.width < BODY_LINE_FRACTION * width:
        return False
    if body_height and not 0.8 * body_height <= line.box.height <= 1.25 * body_height:
        return False
    return _is_loose_text(line, page, column)


def _is_loose_text(line: Line, page: _Page, within: tuple[float, float]) -> bool:
    """Text that is not part of a graphic: not drawn on one, and not framed by one (ink to its
    left and to its right between `within`'s edges, as a box drawn around text in a figure)."""
    if _ink_fraction(page, line.box) >= 0.02:
        return False
    left = _ink_in(page, Box(within[0], line.box.y0, line.box.x0 - 1, line.box.y1))
    right = _ink_in(page, Box(line.box.x1 + 1, line.box.y0, within[1], line.box.y1))
    return not (left.size and left.any() and right.size and right.any())


def _ink_runs(page: _Page, slot: Box) -> list[Box]:
    """Runs of rows with ink inside `slot`, each as the box of its ink."""
    part = _ink_in(page, slot)
    if not part.size:
        return []
    rows = part.any(axis=1)
    runs: list[Box] = []
    start: int | None = None
    for index, has_ink in enumerate([*rows.tolist(), False]):
        if has_ink and start is None:
            start = index
        elif not has_ink and start is not None:
            cols = np.flatnonzero(part[start:index].any(axis=0))
            top = int(slot.y0 * MASK_SCALE)
            left = int(slot.x0 * MASK_SCALE)
            runs.append(
                Box(
                    (left + cols[0]) / MASK_SCALE,
                    (top + start) / MASK_SCALE,
                    (left + cols[-1] + 1) / MASK_SCALE,
                    (top + index) / MASK_SCALE,
                )
            )
            start = None
    return runs


@dataclass(frozen=True)
class _Found:
    box: Box | None  # the region, or None when the candidate is dropped
    limit: Box | None  # how far the crop's margin may reach
    reason: str = ""


def _region(caption: _Caption, page: _Page, body_height: float, *, above: bool) -> _Found:
    x0, x1 = _column(caption, page)
    column = (x0, x1)
    top_margin, bottom_margin = MARGIN * page.height, (1 - MARGIN) * page.height
    in_column = [
        line for line in page.lines
        if line.box.x_overlap(Box(x0, 0, x1, page.height)) > 0.5 * line.box.width
        and line.box.y_overlap(caption.box) <= 0
    ]
    obstacles = [
        line for line in in_column
        if _is_body_line(line, page, column, body_height)
        or CAPTION_RE.match(line.text)
        or OTHER_CAPTION_RE.match(line.text)
    ]
    # The short last line of a paragraph: right below a body line, starting where it starts.
    for line in in_column:  # top to bottom, so a tail found here can anchor the next
        if line in obstacles:
            continue
        if any(
            abs(other.box.x0 - line.box.x0) <= 2.0
            and 0 <= line.box.y0 - other.box.y1 <= 0.6 * other.box.height
            for other in obstacles
        ):
            obstacles.append(line)
    if above:
        edge = max([top_margin, *(o.box.y1 for o in obstacles if o.box.y1 <= caption.box.y0)])
        slot = Box(x0, edge, x1, caption.box.y0)
    else:
        edge = min([bottom_margin, *(o.box.y0 for o in obstacles if o.box.y0 >= caption.box.y1)])
        slot = Box(x0, caption.box.y1, x1, edge)
    if slot.height <= 0:
        return _Found(None, None, "no room for a figure next to the caption")
    ink = _ink_runs(page, slot)
    if not ink:
        return _Found(None, None, "no graphic next to the caption")
    texts = [
        line.box for line in in_column
        if line not in obstacles and line.box.y0 >= slot.y0 - 1 and line.box.y1 <= slot.y1 + 1
    ]
    items = sorted(
        [(box, True) for box in ink] + [(box, False) for box in texts],
        key=lambda item: -item[0].y1 if above else item[0].y0,
    )
    region: Box | None = None
    has_ink = False
    for box, is_ink in items:
        if region is None:
            gap = caption.box.y0 - box.y1 if above else box.y0 - caption.box.y1
            if gap > FIRST_GAP:
                break
        else:
            gap = region.y0 - box.y1 if above else box.y0 - region.y1
            if gap > JOIN_GAP:
                break
            if not is_ink and box.x_gap(region) > SIDE_GAP:
                continue
        region = box if region is None else region.union(box)
        has_ink = has_ink or is_ink
    if region is None or not has_ink:
        return _Found(None, None, "no graphic next to the caption")
    # Text beside the region (an axis label left of the plot, a legend right of it).
    changed = True
    while changed:
        changed = False
        for box in texts:
            if box.y_overlap(region) > 0 and box.x_gap(region) <= SIDE_GAP:
                grown = region.union(box)
                if grown != region:
                    region, changed = grown, True
    return _Found(region, slot)


def _check(region: Box, page: _Page) -> str:
    """Why `region` is not a plausible figure, or "" when it is."""
    page_area = page.width * page.height
    if region.width < MIN_SIDE or region.height < MIN_SIDE or region.area < MIN_AREA_FRACTION * page_area:
        return (
            f"the region found ({region.width:.0f} x {region.height:.0f} pt) is too small to be "
            "a figure"
        )
    if region.area > MAX_PAGE_FRACTION * page_area * (1 - 2 * MARGIN):
        return "the region found covers nearly the whole page"
    ink = _ink_fraction(page, region)
    covered = sum(
        line.box.clip(region).area
        for line in page.lines
        if line.box.x_overlap(region) > 0 and line.box.y_overlap(region) > 0
        and _is_loose_text(line, page, (region.x0, region.x1))
    )
    if ink < TEXT_INK_LIMIT and covered > TEXT_COVER_LIMIT * region.area:
        return "the region found is mostly text"
    return ""


def _body_height(pages: Sequence[_Page]) -> float:
    """The usual height of a line of running text (the median over wide lines)."""
    heights = sorted(
        line.box.height for page in pages for line in page.lines
        if line.box.width >= 0.3 * page.width
    )
    return heights[len(heights) // 2] if heights else 0.0


# -- the whole document ---------------------------------------------------------------------


def _open(path: Path) -> Any:
    try:
        return pdfium.PdfDocument(str(path))
    except (pdfium.PdfiumError, OSError, ValueError) as exc:
        raise FigureError(f"the PDF {path.name} could not be opened: {exc}") from exc


def detect_figures(path: Path) -> FigureScan:
    """Find the figures of the PDF at `path` (not rendered: `asset`, `width`, `height` empty).
    FigureError when the file cannot be opened or a page cannot be read or rendered."""
    document = _open(path)
    masks = _open(path)
    try:
        pages = _read_pages(document, masks)
        figures, dropped = _choose(pages)
        return FigureScan(len(document), tuple(figures), tuple(dropped))
    finally:
        masks.close()
        document.close()


def _read_pages(document: Any, masks: Any) -> list[_Page]:
    pages: list[_Page] = []
    for index in range(len(document)):
        try:
            page = document[index]
            left, bottom, right, top = page.get_cropbox()
            width, height = right - left, top - bottom
            textpage = page.get_textpage()
            try:
                lines = _text_lines(textpage, width, height, left, bottom)
            finally:
                textpage.close()
            info = _Page(index + 1, width, height, lines)
            info.captions = _captions(lines)
            info.two_columns = _two_columns(lines, width)
            if info.captions and page.get_rotation() == 0:
                info.ink = _ink_mask(masks[index], width, height, left, bottom)
            pages.append(info)
        except pdfium.PdfiumError as exc:
            raise FigureError(f"page {index + 1} of the PDF could not be read: {exc}") from exc
    return pages


def _choose(pages: Sequence[_Page]) -> tuple[list[Figure], list[Dropped]]:
    body_height = _body_height(pages)
    kept: dict[str, Figure] = {}
    failed: dict[str, list[Dropped]] = {}
    for page in pages:
        for caption in page.captions:
            if caption.label in kept:
                continue
            reason = ""
            if page.ink is None:
                reason = "the page is rotated, which figure detection does not handle"
            else:
                found = _region(caption, page, body_height, above=True)
                if found.box is None:
                    found = _region(caption, page, body_height, above=False)
                if found.box is None:
                    reason = found.reason
                else:
                    reason = _check(found.box, page)
            if reason:
                failed.setdefault(caption.label, []).append(
                    Dropped(caption.label, page.number, reason)
                )
                continue
            assert found.box is not None and found.limit is not None
            box = _padded(found.box, found.limit, page)
            kept[caption.label] = Figure(
                label=caption.label,
                page=page.number,
                caption=caption.text,
                region=PdfRegion(
                    page=page.number,
                    x0=box.x0 / page.width,
                    y0=box.y0 / page.height,
                    x1=box.x1 / page.width,
                    y1=box.y1 / page.height,
                ),
                box=box,
            )
    figures = sorted(kept.values(), key=lambda f: (f.page, f.box.y0, f.box.x0))
    dropped = [d for label, items in failed.items() if label not in kept for d in items]
    return figures, dropped


def _padded(region: Box, limit: Box, page: _Page) -> Box:
    """`region` with a margin of `PAD`, not beyond `limit` (the slot) or the page."""
    bounds = Box(
        max(0.0, min(limit.x0, region.x0)), max(0.0, min(limit.y0, region.y0)),
        min(page.width, max(limit.x1, region.x1)), min(page.height, max(limit.y1, region.y1)),
    )
    return Box(region.x0 - PAD, region.y0 - PAD, region.x1 + PAD, region.y1 + PAD).clip(bounds)


# -- rendering ------------------------------------------------------------------------------


def render_region(document: Any, page_number: int, box: Box) -> np.ndarray:
    """The pixels (RGB, uint8) of `box` on page `page_number`, trimmed of white borders."""
    page = document[page_number - 1]
    left, bottom, right, top = page.get_cropbox()
    width, height = right - left, top - bottom
    scale = min(RENDER_SCALE, MAX_PIXELS / max(box.width, box.height, 1.0))
    crop = (box.x0, height - box.y1, width - box.x1, box.y0)  # left, bottom, right, top
    bitmap = page.render(scale=scale, crop=crop, rev_byteorder=True, draw_annots=False)
    pixels = np.array(bitmap.to_numpy()[:, :, :3], dtype=np.uint8)
    return _trim(pixels)


def _trim(pixels: np.ndarray) -> np.ndarray:
    dark = (pixels < TRIM_LEVEL).any(axis=2)
    rows = np.flatnonzero(dark.any(axis=1))
    cols = np.flatnonzero(dark.any(axis=0))
    if not rows.size:
        return pixels
    y0 = max(0, rows[0] - TRIM_KEEP)
    y1 = min(pixels.shape[0], rows[-1] + 1 + TRIM_KEEP)
    x0 = max(0, cols[0] - TRIM_KEEP)
    x1 = min(pixels.shape[1], cols[-1] + 1 + TRIM_KEEP)
    return pixels[y0:y1, x0:x1]


def encode_png(pixels: np.ndarray) -> bytes:
    """An 8-bit RGB PNG of `pixels` (rows x cols x 3, uint8), rows filtered with "Up"."""
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype != np.uint8:
        raise ValueError("encode_png needs an array of shape (rows, cols, 3) of uint8")
    rows, cols, _ = pixels.shape
    flat = pixels.reshape(rows, cols * 3)
    up = flat.copy()
    up[1:] = flat[1:] - flat[:-1]  # uint8 arithmetic wraps, as the Up filter needs
    data = np.hstack([np.full((rows, 1), 2, dtype=np.uint8), up]).tobytes()

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(
            ">I", zlib.crc32(kind + body) & 0xFFFFFFFF
        )

    header = struct.pack(">IIBBBBB", cols, rows, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(data, 9))
        + chunk(b"IEND", b"")
    )


def _file_name(label: str, png: bytes) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"{stem}-{hashlib.sha256(png).hexdigest()[:10]}.png"


def extract_figures(path: Path, assets_dir: Path) -> FigureScan:
    """Detect the figures of the PDF at `path` and write their crops into
    `assets_dir/figures/` (emptied first). The returned figures carry their `asset` paths,
    relative to `assets_dir`, and pixel sizes. FigureError for an unreadable PDF."""
    scan = detect_figures(path)
    folder = assets_dir / FIGURES_DIR
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    document = _open(path)
    try:
        rendered = []
        for figure in scan.figures:
            try:
                pixels = render_region(document, figure.page, figure.box)
            except pdfium.PdfiumError as exc:
                raise FigureError(
                    f"{figure.label} on page {figure.page} could not be rendered: {exc}"
                ) from exc
            png = encode_png(pixels)
            name = _file_name(figure.label, png)
            (folder / name).write_bytes(png)
            rendered.append(
                Figure(
                    label=figure.label,
                    page=figure.page,
                    caption=figure.caption,
                    region=figure.region,
                    box=figure.box,
                    asset=f"{FIGURES_DIR}/{name}",
                    width=int(pixels.shape[1]),
                    height=int(pixels.shape[0]),
                )
            )
    finally:
        document.close()
    return FigureScan(scan.pages, tuple(rendered), scan.dropped)


# -- the generator's `figures` stage --------------------------------------------------------

FIGURE_DETECTOR = "pdfium-captions-v1"  # recorded with the stage's output
NOT_A_PDF = "not_a_pdf"
NONE_DETECTED = "none_detected"
FOUND = "found"


@dataclass(frozen=True)
class FiguresResult:
    """What the `figures` stage leaves for the writer: the available figures (rendered, in
    page order) and, when there are none, why (`outcome` and `note`)."""

    outcome: str  # FOUND, NONE_DETECTED or NOT_A_PDF
    note: str
    figures: tuple[Figure, ...] = ()
    dropped: tuple[Dropped, ...] = ()
    pages: int | None = None

    def available(self) -> dict[str, Figure]:
        """The figures the writer may place, by label."""
        return {figure.label: figure for figure in self.figures}

    def record(self, *, slug: str, title: str, media_type: str) -> dict[str, Any]:
        """The content of `generation/figures.json`."""
        return {
            "detector": FIGURE_DETECTOR,
            "source": {"slug": slug, "title": title, "media_type": media_type},
            "outcome": self.outcome,
            "note": self.note,
            "pages": self.pages,
            "figures": [figure.to_dict() for figure in self.figures],
            "dropped": [item.to_dict() for item in self.dropped],
        }


def find_source_figures(path: Path, *, is_pdf: bool, assets_dir: Path) -> FiguresResult:
    """The `figures` stage's work: for a PDF, detect its figures and write their crops into
    `assets_dir/figures/` (`extract_figures`); a source that is not a PDF has none. The folder
    is emptied in every case, so no crop of an earlier run remains. FigureError for a PDF that
    cannot be opened or rendered."""
    if not is_pdf:
        folder = assets_dir / FIGURES_DIR
        if folder.exists():
            shutil.rmtree(folder)
        return FiguresResult(
            NOT_A_PDF,
            "no figures: the source is not a PDF (a page saved from the web arrives as text, "
            "without its images), so it has no figures to show",
        )
    scan = extract_figures(path, assets_dir)
    dropped = f"; {len(scan.dropped)} caption(s) dropped" if scan.dropped else ""
    if not scan.figures:
        return FiguresResult(
            NONE_DETECTED,
            f"no figures: none was detected in the PDF's {scan.pages} page(s){dropped}",
            dropped=scan.dropped,
            pages=scan.pages,
        )
    return FiguresResult(
        FOUND,
        f"{len(scan.figures)} figure(s) found in {scan.pages} page(s){dropped}",
        figures=scan.figures,
        dropped=scan.dropped,
        pages=scan.pages,
    )


def figures_prompt(result: FiguresResult) -> str:
    """The list of available figures for the write prompt (its `<figures>` section): one line
    per figure with its label, page and the source's caption, or a sentence saying that none
    is available and why. Anything in a caption that looks like a tag is escaped."""
    if not result.figures:
        reason = (
            "the document is not a PDF, so it has no figures to show"
            if result.outcome == NOT_A_PDF
            else "no figure could be cut out of this PDF reliably"
        )
        return f"None: {reason}. Do not add any figure block."
    lines = [
        f"- {figure.label} (page {figure.page}): {_neutral(figure.caption)}"
        for figure in result.figures
    ]
    return "\n".join(lines)


def _neutral(text: str) -> str:
    return text.replace("<", "&lt;")


__all__ = [
    "ASSETS_DIR",
    "FIGURES_DIR",
    "FIGURES_FILE",
    "FIGURES_STAGE",
    "FIGURE_DETECTOR",
    "FOUND",
    "NONE_DETECTED",
    "NOT_A_PDF",
    "Box",
    "Dropped",
    "Figure",
    "FigureError",
    "FigureScan",
    "FiguresResult",
    "detect_figures",
    "encode_png",
    "extract_figures",
    "figures_prompt",
    "find_source_figures",
    "render_region",
]
