"""Locator validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from teacher.locators import (
    DiffLocator,
    PdfLocator,
    PdfRegion,
    TextSpanLocator,
    parse_locator,
)

REGION = {"page": 1, "x0": 0.0, "y0": 0.1, "x1": 1.0, "y1": 0.5}


@pytest.mark.parametrize(
    ("data", "cls"),
    [
        ({"type": "pdf_regions", "regions": [REGION]}, PdfLocator),
        ({"type": "pdf_regions", "regions": [REGION, {**REGION, "page": 2}]}, PdfLocator),
        (
            {"type": "diff_lines", "file_path": "a.py", "side": "new", "start_line": 3,
             "end_line": 3},
            DiffLocator,
        ),
        ({"type": "text_span", "start_char": 0, "end_char": 1}, TextSpanLocator),
    ],
)
def test_valid_locators_round_trip(data: dict[str, Any], cls: type) -> None:
    loc = parse_locator(data)
    assert isinstance(loc, cls)
    dumped = loc.model_dump()
    assert dumped["type"] == data["type"]
    assert parse_locator(dumped) == loc
    assert parse_locator(loc) == loc


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"type": "unknown"},
        {"start_char": 0, "end_char": 1},  # missing discriminator
        {"type": "pdf_regions", "regions": []},
        {"type": "pdf_regions", "regions": [{**REGION, "page": 0}]},
        {"type": "pdf_regions", "regions": [{**REGION, "x1": 1.5}]},
        {"type": "pdf_regions", "regions": [{**REGION, "y0": -0.1}]},
        {"type": "pdf_regions", "regions": [{**REGION, "x0": 0.5, "x1": 0.5}]},
        {"type": "pdf_regions", "regions": [{**REGION, "y0": 0.6, "y1": 0.5}]},
        {"type": "diff_lines", "file_path": "a.py", "side": "both", "start_line": 1,
         "end_line": 2},
        {"type": "diff_lines", "file_path": "a.py", "side": "old", "start_line": 0,
         "end_line": 2},
        {"type": "diff_lines", "file_path": "a.py", "side": "old", "start_line": 5,
         "end_line": 4},
        {"type": "diff_lines", "side": "old", "start_line": 1, "end_line": 2},
        {"type": "text_span", "start_char": -1, "end_char": 3},
        {"type": "text_span", "start_char": 3, "end_char": 3},
        {"type": "text_span", "start_char": 0, "end_char": 3, "extra": 1},
    ],
)
def test_invalid_locators_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_locator(data)


def test_models_constructible_directly() -> None:
    loc = PdfLocator(regions=[PdfRegion(**REGION)])
    assert loc.type == "pdf_regions"
    assert loc.model_dump()["regions"][0]["page"] == 1
    with pytest.raises(ValidationError):
        TextSpanLocator(start_char=5, end_char=2)
