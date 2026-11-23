"""Figures of a source PDF (`teacher.figures`): detection, cropping, the stage's result and the
write prompt's list, on small PDFs built here with pypdfium2 (text lines, filled rectangles as
vector drawings, an image); the stored `figure` block, the writer's figure blocks and their
page rules, the consumers of block types, and the generator's `figures` stage."""

from __future__ import annotations

import ctypes
import io
import struct
import zlib
import json
import random
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import anthropic
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from teacher.block_tagging import PageBlock, blocks_payload
from teacher.blocks import (
    block_label,
    is_gate_type,
    is_question_type,
    private_content,
    public_content,
    validate_block_content,
)
from teacher.claude_generator import (
    WRITE_INSTRUCTIONS,
    ClaudeGenerator,
    GenerationError,
    StudyPage,
    block_content,
    check_page,
    page_violations,
    render_study_page,
    to_draft,
)
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.figures import (
    FIGURES_DIR,
    FOUND,
    NONE_DETECTED,
    NOT_A_PDF,
    Box,
    Figure,
    FigureError,
    FiguresResult,
    detect_figures,
    encode_png,
    extract_figures,
    figures_prompt,
    find_source_figures,
)
from teacher.generation import run_generation
from teacher.glossary import block_fields
from teacher.locators import PdfRegion
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    GenerationRunManager,
    KnowledgeSourceManager,
)
from teacher.search import _block_units
from teacher.tutor import SYSTEM_PROMPT as TUTOR_SYSTEM_PROMPT
from teacher.tutor import VisibleBlock, turn_text

WIDTH, HEIGHT = 612.0, 792.0
BODY = 10.0  # body font size
LOREM = (
    "Running text of the paper fills the column from margin to margin so that it reads as body"
)


# -- building PDFs --------------------------------------------------------------------------


class Page:
    """Items to draw on one page, in top-left coordinates (points)."""

    def __init__(self) -> None:
        self.items: list[tuple] = []

    def text(self, x: float, top: float, text: str, size: float = BODY) -> Page:
        self.items.append(("text", x, top, text, size))
        return self

    def paragraph(self, x: float, top: float, lines: int, width_chars: int = 95) -> Page:
        for n in range(lines):
            self.text(x, top + n * BODY * 1.25, LOREM[:width_chars])
        return self

    def rect(self, x0: float, top: float, x1: float, bottom: float,
             rgb: tuple[int, int, int] = (40, 90, 160)) -> Page:
        self.items.append(("rect", x0, top, x1, bottom, rgb))
        return self

    def image(self, x0: float, top: float, x1: float, bottom: float) -> Page:
        """A placed image that is white except for a small dark mark in its middle."""
        self.items.append(("image", x0, top, x1, bottom))
        return self


def build_pdf(path: Path, pages: Sequence[Page]) -> Path:
    document = pdfium.PdfDocument.new()
    for spec in pages:
        page = document.new_page(WIDTH, HEIGHT)
        for item in spec.items:
            kind = item[0]
            if kind == "text":
                _, x, top, text, size = item
                obj = pdfium_c.FPDFPageObj_NewTextObj(document.raw, b"Helvetica", ctypes.c_float(size))
                encoded = (text + "\0").encode("utf-16-le")
                buffer = ctypes.create_string_buffer(encoded, len(encoded))
                pdfium_c.FPDFText_SetText(obj, ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR)))
                baseline = HEIGHT - top - size * 0.8
                pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, x, baseline)
                pdfium_c.FPDFPage_InsertObject(page.raw, obj)
            elif kind == "rect":
                _, x0, top, x1, bottom, (r, g, b) = item
                obj = pdfium_c.FPDFPageObj_CreateNewRect(
                    ctypes.c_float(x0), ctypes.c_float(HEIGHT - bottom),
                    ctypes.c_float(x1 - x0), ctypes.c_float(bottom - top),
                )
                pdfium_c.FPDFPageObj_SetFillColor(obj, r, g, b, 255)
                pdfium_c.FPDFPath_SetDrawMode(obj, pdfium_c.FPDF_FILLMODE_ALTERNATE, False)
                pdfium_c.FPDFPage_InsertObject(page.raw, obj)
            elif kind == "image":
                _, x0, top, x1, bottom = item
                pixels = np.full((40, 40, 3), 255, dtype=np.uint8)
                pixels[18:22, 18:22] = 0
                bitmap = pdfium.PdfBitmap.new_native(40, 40, pdfium_c.FPDFBitmap_BGR)
                bitmap.to_numpy()[:] = pixels
                image = pdfium.PdfImage.new(document)
                image.set_bitmap(bitmap)
                image.set_matrix(pdfium.PdfMatrix().scale(x1 - x0, bottom - top).translate(
                    x0, HEIGHT - bottom
                ))
                page.insert_obj(image)
        page.gen_content()
    out = io.BytesIO()
    document.save(out)
    path.write_bytes(out.getvalue())
    return path


def decode_png(data: bytes) -> np.ndarray:
    """The RGB pixels of a PNG written by `encode_png` (one IDAT, "Up" filter on every row)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", (0, 0)
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == struct.unpack(
            ">I", data[pos + 8 + length:pos + 12 + length]
        )[0]
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
            assert body[8:] == bytes([8, 2, 0, 0, 0])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    cols, rows = size
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(rows, 1 + cols * 3)
    assert (raw[:, 0] == 2).all()
    out = raw[:, 1:].copy()
    for row in range(1, rows):
        out[row] = out[row] + out[row - 1]
    return out.reshape(rows, cols, 3)


def figure_page(caption: str = "Figure 1: A test diagram of the method.") -> Page:
    """Body text, a two-part drawing, its caption below, more body text."""
    page = Page().paragraph(72, 72, 6)
    page.rect(150, 200, 300, 300).rect(320, 230, 460, 330, (200, 60, 40))
    page.text(330, 335, "axis label", size=7)  # figure text right below the drawing
    page.text(72, 360, caption)
    page.paragraph(72, 400, 8)
    return page


# -- detection ------------------------------------------------------------------------------


def test_detects_a_figure_above_its_caption(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "paper.pdf", [Page().paragraph(72, 72, 30), figure_page()])
    scan = detect_figures(pdf)
    assert scan.pages == 2 and scan.dropped == ()
    [figure] = scan.figures
    assert figure.label == "Figure 1" and figure.page == 2
    assert figure.caption == "A test diagram of the method."
    box = figure.box
    # The whole drawing and its label, nothing of the body text above or of the caption below.
    assert box.x0 <= 150 and box.x1 >= 460 and box.y0 <= 200 and box.y1 >= 340
    assert box.y0 > 72 + 6 * BODY * 1.25 and box.y1 < 360
    region = figure.region
    assert region.page == 2
    assert region.x0 == pytest.approx(box.x0 / WIDTH) and region.y1 == pytest.approx(box.y1 / HEIGHT)


def test_caption_above_its_figure(tmp_path: Path) -> None:
    page = Page().paragraph(72, 72, 4)
    page.text(72, 150, "Fig. 2. The pipeline, drawn below its caption.")
    page.rect(150, 175, 450, 300)
    page.paragraph(72, 340, 6)
    scan = detect_figures(build_pdf(tmp_path / "p.pdf", [page]))
    [figure] = scan.figures
    assert figure.label == "Figure 2"  # "Fig. 2" is the same label
    assert figure.caption == "The pipeline, drawn below its caption."
    assert figure.box.y0 >= 160 and figure.box.y1 <= 310 and figure.box.y0 <= 175


def test_a_caption_runs_over_several_lines(tmp_path: Path) -> None:
    page = Page().paragraph(72, 72, 4)
    page.rect(150, 160, 450, 300)
    page.text(72, 320, "Figure 3: The first line of a caption that goes")
    page.text(72, 332.5, "on over a second line.")
    page.paragraph(72, 370, 6)
    [figure] = detect_figures(build_pdf(tmp_path / "p.pdf", [page])).figures
    assert figure.caption == "The first line of a caption that goes on over a second line."


def test_references_in_the_body_are_not_captions(tmp_path: Path) -> None:
    page = Page().paragraph(72, 72, 3)
    page.text(72, 120, "Figure 2 shows the result of the method on every benchmark we tried.")
    page.text(72, 140, "Figure 4(a) compares the two variants of the method on the benchmark.")
    page.paragraph(72, 160, 4)
    page.text(72, 220, "Figure 5. Then the method is applied to a further set of problems here.")
    page.paragraph(72, 232.5, 4)
    scan = detect_figures(build_pdf(tmp_path / "p.pdf", [page]))
    assert scan.figures == ()
    # Only the line that looks like a caption is a candidate, and it has no graphic.
    assert [(d.label, d.reason) for d in scan.dropped] == [
        ("Figure 5", "no graphic next to the caption")
    ]


def test_two_columns_keep_their_figures_apart(tmp_path: Path) -> None:
    page = Page()
    for column in (54, 316):
        page.paragraph(column, 72, 8, width_chars=48)
    page.rect(80, 200, 270, 300).text(54, 320, "Figure 1: Left.")
    page.rect(340, 200, 540, 320).text(316, 340, "Figure 2: Right.")
    for column in (54, 316):
        page.paragraph(column, 380, 10, width_chars=48)
    scan = detect_figures(build_pdf(tmp_path / "p.pdf", [page]))
    left, right = scan.figures
    assert (left.label, right.label) == ("Figure 1", "Figure 2")
    assert left.box.x1 < 306 and left.box.x0 <= 80 and left.box.x1 >= 270
    assert right.box.x0 > 306 and right.box.x0 <= 340 and right.box.x1 >= 540


def test_an_image_counts_whole_white_parts_included(tmp_path: Path) -> None:
    """A screenshot's white background belongs to it, so the crop is the image's rectangle and
    the caption, far below the dark part, still finds it."""
    page = Page().paragraph(72, 72, 4)
    page.image(120, 150, 480, 330)
    page.text(72, 345, "Figure 1: A screenshot of the tool.")
    page.paragraph(72, 380, 6)
    [figure] = detect_figures(build_pdf(tmp_path / "p.pdf", [page])).figures
    assert figure.box.x0 <= 120 and figure.box.x1 >= 480
    assert figure.box.y0 <= 150 and figure.box.y1 >= 330


def test_doubtful_candidates_are_dropped_with_reasons(tmp_path: Path) -> None:
    small = Page().paragraph(72, 72, 4).rect(300, 150, 320, 165).text(72, 175, "Figure 1: Tiny.")
    small.paragraph(72, 200, 4)
    whole = Page().rect(20, 50, 592, 740).text(72, 750, "Figure 2: A full-page drawing.")
    scan = detect_figures(build_pdf(tmp_path / "p.pdf", [small, whole]))
    assert scan.figures == ()
    reasons = {d.label: d.reason for d in scan.dropped}
    assert "too small" in reasons["Figure 1"]
    assert reasons["Figure 2"] == "the region found covers nearly the whole page"


def test_a_region_of_loose_text_is_dropped(tmp_path: Path) -> None:
    """Small unframed text under a thin rule is a listing, not a picture: mostly text."""
    page = Page().paragraph(72, 72, 4)
    page.rect(150, 140, 450, 142)
    for n in range(8):
        page.text(150, 146 + n * 8, LOREM[:80], size=6.5)
    page.text(72, 225, "Figure 1: A listing set as a figure.")
    page.paragraph(72, 260, 6)
    scan = detect_figures(build_pdf(tmp_path / "p.pdf", [page]))
    assert scan.figures == ()
    assert [(d.label, d.reason) for d in scan.dropped] == [
        ("Figure 1", "the region found is mostly text")
    ]


def test_framed_text_is_a_figure(tmp_path: Path) -> None:
    """Text inside a drawn box (an excerpt set as a figure) is part of the figure, even at body
    size and width, and is not taken for running text."""
    page = Page().paragraph(72, 72, 4)
    page.rect(100, 140, 512, 141).rect(100, 260, 512, 261)  # top and bottom of the frame
    page.rect(100, 140, 101, 261).rect(511, 140, 512, 261)  # its sides
    page.paragraph(110, 150, 8, width_chars=88)
    page.text(72, 275, "Figure 1: An excerpt of the instructions.")
    page.paragraph(72, 310, 6)
    [figure] = detect_figures(build_pdf(tmp_path / "p.pdf", [page])).figures
    assert figure.box.y0 <= 140 and figure.box.y1 >= 261


def test_the_first_good_candidate_of_a_label_wins(tmp_path: Path) -> None:
    """A body line that looks like a caption earlier in the paper is dropped silently once the
    real caption of that label is found."""
    early = Page().paragraph(72, 72, 3).text(72, 120, "Figure 1. As we will see, it works.")
    early.paragraph(72, 132.5, 6)
    scan = detect_figures(build_pdf(tmp_path / "p.pdf", [early, figure_page()]))
    assert [(f.label, f.page) for f in scan.figures] == [("Figure 1", 2)]
    assert scan.dropped == ()


def test_unreadable_pdf_fails_loudly(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n" + b"\x00garbage" * 50)
    with pytest.raises(FigureError, match="could not be opened"):
        detect_figures(broken)
    with pytest.raises(FigureError):
        find_source_figures(broken, is_pdf=True, assets_dir=tmp_path / "assets")


# -- crops ----------------------------------------------------------------------------------


def test_encode_png_round_trips() -> None:
    rng = np.random.default_rng(0)
    pixels = rng.integers(0, 256, size=(7, 5, 3), dtype=np.uint8)
    assert (decode_png(encode_png(pixels)) == pixels).all()
    with pytest.raises(ValueError):
        encode_png(pixels.astype(np.int16))


def test_extract_writes_trimmed_crops(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "paper.pdf", [figure_page()])
    assets = tmp_path / "assets"
    (assets / FIGURES_DIR).mkdir(parents=True)
    stale = assets / FIGURES_DIR / "figure-1-old.png"
    stale.write_bytes(b"stale")
    other = assets / "notes.txt"
    other.write_text("not the stage's")
    scan = extract_figures(pdf, assets)
    [figure] = scan.figures
    assert not stale.exists() and other.exists()  # only the stage's own folder is emptied
    assert figure.asset.startswith(f"{FIGURES_DIR}/figure-1-") and figure.asset.endswith(".png")
    pixels = decode_png((assets / figure.asset).read_bytes())
    assert pixels.shape == (figure.height, figure.width, 3)
    # 144 dpi: about two pixels per point of the drawing (150..460 x 200..340), trimmed.
    assert 600 <= figure.width <= 660 and 270 <= figure.height <= 320
    # Trimmed to the drawing with a thin white border: the blue part reaches near the edge.
    blue = np.argwhere((pixels[:, :, 2] > 120) & (pixels[:, :, 0] < 80))
    assert blue[:, 1].min() <= 10 and blue[:, 0].min() <= 10


# -- the stage's result and the prompt ------------------------------------------------------


def test_stage_result_for_a_pdf_with_figures(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "paper.pdf", [figure_page("Figure 1: Uses a < b in its text.")])
    result = find_source_figures(pdf, is_pdf=True, assets_dir=tmp_path / "assets")
    assert result.outcome == FOUND and result.note == "1 figure(s) found in 1 page(s)"
    assert list(result.available()) == ["Figure 1"]
    record = result.record(slug="paper", title="Paper", media_type="application/pdf")
    assert record["outcome"] == FOUND and record["pages"] == 1 and record["dropped"] == []
    [entry] = record["figures"]
    assert set(entry) == {"label", "page", "caption", "region", "asset", "width", "height"}
    assert figures_prompt(result) == "- Figure 1 (page 1): Uses a &lt; b in its text."


def test_stage_result_when_nothing_is_detected(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "paper.pdf", [Page().paragraph(72, 72, 20)])
    result = find_source_figures(pdf, is_pdf=True, assets_dir=tmp_path / "assets")
    assert result.outcome == NONE_DETECTED and result.figures == ()
    assert result.note == "no figures: none was detected in the PDF's 1 page(s)"
    assert figures_prompt(result).startswith("None: no figure could be cut out of this PDF")
    assert "Do not add any figure block" in figures_prompt(result)
    assert (tmp_path / "assets" / FIGURES_DIR).is_dir()
    assert list((tmp_path / "assets" / FIGURES_DIR).iterdir()) == []


def test_stage_result_for_a_text_source(tmp_path: Path) -> None:
    text = tmp_path / "page.txt"
    text.write_text("Saved from the web.\n")
    stale = tmp_path / "assets" / FIGURES_DIR / "x.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    result = find_source_figures(text, is_pdf=False, assets_dir=tmp_path / "assets")
    assert result.outcome == NOT_A_PDF and result.available() == {}
    assert "not a PDF" in result.note and not stale.exists()
    assert figures_prompt(result) == (
        "None: the document is not a PDF, so it has no figures to show. Do not add any figure "
        "block."
    )
    assert result.record(slug="s", title="T", media_type="text/plain")["pages"] is None


def test_figures_result_is_plain_data() -> None:
    result = FiguresResult(NONE_DETECTED, "no figures", pages=3)
    assert result.available() == {} and result.record(
        slug="s", title="T", media_type="application/pdf"
    )["figures"] == []


# -- the stored block type ------------------------------------------------------------------

FIGURE_CONTENT: dict = {
    "asset": "figures/figure-1-0123456789.png",
    "label": "Figure 1",
    "source_caption": "A test diagram of the method.",
    "page": 2,
    "width": 640,
    "height": 300,
    "caption": "Follow the **arrows** from the encoder to the decoder.",
}


def test_figure_block_type() -> None:
    assert validate_block_content("figure", dict(FIGURE_CONTENT)) == FIGURE_CONTENT
    assert not is_gate_type("figure") and not is_question_type("figure")
    assert public_content(FIGURE_CONTENT) == FIGURE_CONTENT and private_content(FIGURE_CONTENT) is None
    assert block_label("figure", FIGURE_CONTENT) == (
        "Figure 1: Follow the arrows from the encoder to the decoder."
    )
    # The glossary reads the text, never the image's path.
    assert [name for name, _text, _private in block_fields(FIGURE_CONTENT)] == [
        "label", "source_caption", "caption"
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"asset": "../outside.png"}, "relative path"),
        ({"asset": "/etc/figure.png"}, "relative path"),
        ({"asset": "figures/figure.svg"}, "ending in .png"),
        ({"label": " "}, "label must not be blank"),
        ({"source_caption": ""}, "source_caption must not be blank"),
        ({"caption": "  "}, "caption must not be blank"),
        ({"page": 0}, "page"),
        ({"width": "640"}, "width"),
        ({"height": 0}, "height"),
        ({"alt": "x"}, "alt"),
    ],
)
def test_figure_block_rejects(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_block_content("figure", {**FIGURE_CONTENT, **change})
    missing = dict(FIGURE_CONTENT)
    del missing["caption"]
    with pytest.raises(ValueError, match="caption"):
        validate_block_content("figure", missing)


# -- the writer's figure blocks and the page rules ------------------------------------------


def available() -> dict[str, Figure]:
    box = Box(100.0, 150.0, 400.0, 300.0)
    region = PdfRegion(page=2, x0=0.1, y0=0.2, x1=0.6, y1=0.4)
    return {
        label: Figure(label, page, caption, region, box, asset, 640, 300)
        for label, page, caption, asset in (
            ("Figure 1", 2, "A test diagram of the method.", "figures/figure-1-aa.png"),
            ("Figure 2", 3, "Results on the benchmark.", "figures/figure-2-bb.png"),
        )
    }


def prose(text: str = "The method has two parts, shown in the figure below.",
          role: str = "explanation") -> dict:
    return {"type": "prose", "section": "" if role == "critique" else "method", "role": role,
            "markdown": text}


def figure(label: str = "Figure 1", caption: str = "Look at the two parts.") -> dict:
    return {"type": "figure", "section": "method", "figure": label, "caption": caption}


def figure_violations(blocks: list[dict], figures: dict | None) -> list[str]:
    """The violations that name a figure block (the rest of this small page breaks other
    rules, which other tests cover)."""
    page = StudyPage.model_validate({"blocks": blocks})
    numbers = {n for n, b in enumerate(blocks, start=1) if b["type"] == "figure"}
    return [
        v for v in page_violations(page, ["method"], figures)
        if any(v.startswith(f"block {n} (figure ") for n in numbers)
    ]


def test_a_well_placed_figure_breaks_no_rule() -> None:
    blocks = [prose(), figure(), prose("More."), figure("Figure 2", "The bars.")]
    assert figure_violations(blocks, available()) == []
    # Two figures after one explanation are fine too.
    assert figure_violations([prose(), figure(), figure("Figure 2")], available()) == []


def test_a_figure_must_be_available() -> None:
    [violation] = figure_violations([prose(), figure("Figure 9")], available())
    assert violation.startswith("block 2 (figure 'Figure 9'): figure 'Figure 9' is not one of")
    assert "available: 'Figure 1', 'Figure 2'" in violation
    # Without figures (a text source, or none detected) no figure block is allowed.
    [violation] = figure_violations([prose(), figure()], None)
    assert "(available: none)" in violation


def test_a_figure_appears_once() -> None:
    [violation] = figure_violations([prose(), figure(), prose("Again."), figure()], available())
    assert violation.startswith("block 4 (figure 'Figure 1'): 'Figure 1' is already shown by "
                                "block 2")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ([], "it is the first block"),
        ([prose("A critical look.", role="critique")], "block 1 is a critique"),
        ([prose(), {"type": "prediction", "section": "method", "prompt": "Guess?",
                    "options": ["Up", "Down"], "correct_index": 0, "resolution": "Up."}],
         "block 2 is a prediction"),
    ],
)
def test_a_figure_follows_the_explanation_that_introduces_it(before: list, after: str) -> None:
    blocks = [*before, figure()]
    # (A page that starts with a figure also breaks the rule that it starts with an explanation.)
    [violation] = [v for v in figure_violations(blocks, available()) if "introduces it" in v]
    assert "a figure comes right after the explanation that introduces it" in violation
    assert violation.endswith(after)


def test_check_page_names_figure_problems() -> None:
    page = StudyPage.model_validate({"blocks": [prose(), figure("Figure 7")]})
    with pytest.raises(GenerationError, match="'Figure 7' is not one of the figures available"):
        check_page(page, ["method"], available())


def test_to_draft_takes_the_image_from_the_figures_stage() -> None:
    [block] = StudyPage.model_validate({"blocks": [figure()]}).blocks
    draft = to_draft(block, {"method": 5}, random.Random(0), available())
    assert draft.type == "figure" and draft.segment_ids == (5,)
    assert draft.content == {
        "asset": "figures/figure-1-aa.png",
        "label": "Figure 1",
        "source_caption": "A test diagram of the method.",
        "page": 2,
        "width": 640,
        "height": 300,
        "caption": "Look at the two parts.",
    }
    assert validate_block_content("figure", draft.content) == draft.content
    with pytest.raises(ValueError, match="not one of the figures available"):
        to_draft(block, {}, random.Random(0))
    assert block_content(block, available())["label"] == "Figure 1"


def test_the_write_prompt_has_a_place_for_the_figures() -> None:
    text = WRITE_INSTRUCTIONS.format(depth="short", depth_rule="r", outline="{}", figures="LIST")
    assert text.endswith("<figures>\nLIST\n</figures>")
    assert "cannot display them yet" not in text
    for rule in ("at most once", "right after the explanation that introduces it",
                 "usually one to three on a"):
        assert rule in " ".join(text.split()), rule


# -- consumers of block types ---------------------------------------------------------------


def test_search_indexes_a_figures_text() -> None:
    units: list = []
    block = SimpleNamespace(key="b3", type="figure")
    section = _block_units(units, block, FIGURE_CONTENT, "Method", False)
    assert section == "Method"
    assert [(u.field, u.text, u.block) for u in units] == [
        ("body", "Figure 1", "b3"),
        ("body", "Follow the arrows from the encoder to the decoder.", "b3"),
        ("body", "A test diagram of the method.", "b3"),
    ]


def test_glossary_step_sees_the_figures_text_not_its_path() -> None:
    rendered = render_study_page([("figure", FIGURE_CONTENT)])
    assert '<block number="1" type="figure">' in rendered
    assert "<source_caption>\nA test diagram of the method.\n</source_caption>" in rendered
    assert "figures/" not in rendered and "640" not in rendered


def test_tagger_sees_a_figure_by_its_label_and_captions() -> None:
    [item] = blocks_payload([PageBlock(1, "b1", "figure", FIGURE_CONTENT)])
    assert item["content"] == {
        "label": "Figure 1",
        "source_caption": "A test diagram of the method.",
        "caption": "Follow the **arrows** from the encoder to the decoder.",
    }


def test_tutor_sees_a_figure_by_its_label_page_and_captions() -> None:
    block = VisibleBlock(key="b2", type="figure", gate_state=None, public=dict(FIGURE_CONTENT),
                         revealed=None)
    text, context = turn_text([block], [], kind="message", text="What does it show?",
                              references=[], locked=0)
    assert context == [{"key": "b2", "revealed": False}]
    assert '<block key="b2" type="figure">' in text
    assert '"source_caption": "A test diagram of the method."' in text
    assert '"page": 2' in text and '"caption": "Follow the **arrows**' in text
    assert "figures/" not in text and "640" not in text
    assert "Of a figure you are given its label" in TUTOR_SYSTEM_PROMPT


def test_history_names_a_figure_block(settings: Settings) -> None:
    """The study history's time per block labels a figure by its label and caption."""
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).create(
            slug="page", kind="study_page", title="Study", generator="test"
        )
        block = ArtifactBlockManager(session, settings).add(
            artifact, type="figure", content=dict(FIGURE_CONTENT)
        )
        label = block_label(block.type, ArtifactBlockManager(session, settings).read_content(block))
    assert label == "Figure 1: Follow the arrows from the encoder to the decoder."


# -- the generator's figures stage ----------------------------------------------------------

STAGE_OUTLINE = {
    "summary": "The paper proposes a method.",
    "main_claims": ["It works."],
    "background": ["algebra"],
    "sections": [{"label": "method", "title": "Method", "start_page": 1, "end_page": 1,
                  "key_points": ["How it works."]}],
}


class _Available:
    """A concept or tagging stage that is available and never reached (the write call fails
    first)."""

    model = "none"
    name = "unused"

    def check_available(self) -> None:
        return None


def _generate_until_write(settings: Settings, tmp_path: Path, fake_anthropic, content: bytes,
                          filename: str, media_type: str) -> tuple[str | None, object]:
    upload = tmp_path / "upload" / filename
    upload.parent.mkdir()
    upload.write_bytes(content)
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(
            upload, slug="paper", kind="paper", title="Paper", media_type=media_type
        )
        ArtifactManager(session, settings).create(
            slug="page", kind="study_page", title="Study", generator="claude-study-v1",
            sources=[(source, "primary")],
        )
    outline = dict(STAGE_OUTLINE)
    if media_type != "application/pdf":
        outline["sections"] = [{**outline["sections"][0], "start_page": None, "end_page": None}]
    fake = fake_anthropic(
        fake_anthropic.reply(text=json.dumps(outline)),
        fake_anthropic.status_error(anthropic.InternalServerError, 500),
    )
    generator = ClaudeGenerator(fake, concept_extractor=_Available(), block_tagger=_Available())
    return run_generation(settings, "page", generator), fake


def _runs(settings: Settings) -> list:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug("page")
        return [(r.stage, r.status, r.note, r.output_file, r.error)
                for r in GenerationRunManager(session, settings).list(artifact)]


def _write_prompt(fake) -> str:
    return fake.calls[-1]["messages"][0]["content"][1]["text"]


def test_stage_crops_the_figures_and_lists_them_for_the_writer(
    settings: Settings, tmp_path: Path, fake_anthropic
) -> None:
    pdf = build_pdf(tmp_path / "paper.pdf", [figure_page()]).read_bytes()
    status, fake = _generate_until_write(settings, tmp_path, fake_anthropic, pdf, "paper.pdf",
                                         "application/pdf")
    assert status == "failed"  # at write, which the fake fails
    stages = _runs(settings)
    assert [(s, st) for s, st, *_ in stages][:3] == [
        ("preflight", "succeeded"), ("analyse", "succeeded"), ("figures", "succeeded")
    ]
    _stage, _status, note, output_file, _error = stages[2]
    assert note == "1 figure(s) found in 1 page(s)"
    assert output_file == "artifacts/page/generation/figures.json"
    record = json.loads((settings.root / output_file).read_text(encoding="utf-8"))
    assert record["outcome"] == "found" and record["source"]["slug"] == "paper"
    [entry] = record["figures"]
    assert entry["label"] == "Figure 1" and entry["caption"] == "A test diagram of the method."
    crop = settings.artifacts_dir / "page" / "assets" / entry["asset"]
    assert decode_png(crop.read_bytes()).shape == (entry["height"], entry["width"], 3)
    # (The reader profile follows the instructions, so the list is not at the very end.)
    assert "<figures>\n- Figure 1 (page 1): A test diagram of the method.\n</figures>" in (
        _write_prompt(fake)
    )


def test_stage_on_a_text_source_says_there_are_no_figures(
    settings: Settings, tmp_path: Path, fake_anthropic
) -> None:
    status, fake = _generate_until_write(settings, tmp_path, fake_anthropic, b"# Notes\n\nText.\n",
                                         "notes.md", "text/markdown")
    assert status == "failed"
    _stage, state, note, output_file, _error = _runs(settings)[2]
    assert state == "succeeded" and note.startswith("no figures: the source is not a PDF")
    record = json.loads((settings.root / output_file).read_text(encoding="utf-8"))
    assert record["outcome"] == "not_a_pdf" and record["figures"] == []
    assert "<figures>\nNone: the document is not a PDF" in _write_prompt(fake)


def test_stage_fails_loudly_on_a_pdf_it_cannot_open(
    settings: Settings, tmp_path: Path, fake_anthropic
) -> None:
    status, _fake = _generate_until_write(settings, tmp_path, fake_anthropic,
                                          b"%PDF-1.4\n" + b"\x00garbage" * 50, "paper.pdf",
                                          "application/pdf")
    assert status == "failed"
    stage, state, _note, _file, error = _runs(settings)[-1]
    assert (stage, state) == ("figures", "failed")
    assert error.startswith("GenerationError: the source's figures could not be read: ")
