"""Small helpers shared by the routers' response models.

`CostOut` (and its `CostPartOut` lines) is how the API shows a cost-ledger total
(`teacher.costs.CostTotal`): `total_usd` sums the calls with a known price; `calls` counts all
of them, `unknown_price_calls` those with no known price (not in the total; their models in
`unknown_price_models`); `estimate` says the total includes upper-bound estimates
(`estimate_calls` of them, backfilled calls of which only combined token counts were kept), so
the total is then itself an upper bound; `parts` is the breakdown (by purpose, or by stage for
a generation), most expensive first.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from teacher.costs import CostTotal


def to_iso(value: datetime | None) -> str | None:
    """ISO 8601 string for a stored (naive UTC) timestamp, with an explicit `+00:00` offset."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class CostPartOut(BaseModel):
    key: str  # the purpose (e.g. "tutor") or the generation stage (e.g. "write")
    label: str
    total_usd: float
    calls: int
    unknown_price_calls: int
    estimate: bool


class CostOut(BaseModel):
    total_usd: float
    calls: int
    unknown_price_calls: int
    unknown_price_models: list[str]
    estimate: bool
    estimate_calls: int
    parts: list[CostPartOut]

    @classmethod
    def of(cls, total: CostTotal) -> CostOut:
        return cls.model_validate(total.to_dict())


__all__ = ["CostOut", "CostPartOut", "to_iso"]
