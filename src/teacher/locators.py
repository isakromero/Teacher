"""Pydantic locator types: where a source segment lives inside its knowledge source.

`source_segments.locator_type` stores the `type` discriminator; `source_segments.locator` stores
`locator.model_dump()` (which includes `type`).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfRegion(_Strict):
    """A rectangle on a PDF page, as fractions of the page size, origin top-left."""

    page: int = Field(ge=1)
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _check_order(self) -> PdfRegion:
        if not self.x0 < self.x1:
            raise ValueError("x0 must be < x1")
        if not self.y0 < self.y1:
            raise ValueError("y0 must be < y1")
        return self


class PdfLocator(_Strict):
    type: Literal["pdf_regions"] = "pdf_regions"
    regions: list[PdfRegion] = Field(min_length=1)


class DiffLocator(_Strict):
    type: Literal["diff_lines"] = "diff_lines"
    file_path: str
    side: Literal["old", "new"]
    start_line: int = Field(ge=1)
    end_line: int

    @model_validator(mode="after")
    def _check_range(self) -> DiffLocator:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class TextSpanLocator(_Strict):
    type: Literal["text_span"] = "text_span"
    start_char: int = Field(ge=0)
    end_char: int

    @model_validator(mode="after")
    def _check_range(self) -> TextSpanLocator:
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be > start_char")
        return self


Locator = Annotated[PdfLocator | DiffLocator | TextSpanLocator, Field(discriminator="type")]

_adapter: TypeAdapter[Locator] = TypeAdapter(Locator)


def parse_locator(data: dict[str, Any] | Locator) -> Locator:
    """Validate a dict (or re-validate a model) into a Locator; raises `pydantic.ValidationError`."""
    if isinstance(data, BaseModel):
        data = data.model_dump()
    return _adapter.validate_python(data)


__all__ = [
    "DiffLocator",
    "Locator",
    "PdfLocator",
    "PdfRegion",
    "TextSpanLocator",
    "parse_locator",
]
