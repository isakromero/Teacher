"""Small helpers shared by the routers' response models."""

from __future__ import annotations

from datetime import UTC, datetime


def to_iso(value: datetime | None) -> str | None:
    """ISO 8601 string for a stored (naive UTC) timestamp, with an explicit `+00:00` offset."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


__all__ = ["to_iso"]
