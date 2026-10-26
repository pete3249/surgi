"""Pydantic models describe the shape of our data and validate it for us.

The browser and the cache files use camelCase JSON keys (generatedAt, fromCache), while
Python style is snake_case. `CamelModel` converts between the two automatically, so in
Python you write `plan.generated_at` and the JSON still says "generatedAt".
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool, field_serializer
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PlanRequest(CamelModel):
    """Body of POST /api/plan. Unknown keys are ignored (older pages also sent a "part")."""

    surgery: str = ""
    refresh: StrictBool = False  # only a real JSON true counts, not "true" or 1


class PlanItem(CamelModel):
    id: str
    phase: Literal["pre", "post"]
    cat: str
    d: int  # days relative to surgery: negative = before, 0 = surgery day, positive = after
    text: str
    detail: str
    tier: Literal["need", "nice"] | None = None  # only for equipment


class PlanStats(CamelModel):
    dropped: int = 0
    duplicates: int = 0


class Plan(CamelModel):
    source: str = "ai"
    surgery: str
    expect: str
    items: list[PlanItem]
    model: str
    generated_at: datetime
    stats: PlanStats
    from_cache: bool = False
    reviewed: bool = False  # True only for plans read and approved by a person (the files in plans/)

    @field_serializer("generated_at")
    def _ser_generated_at(self, v: datetime) -> str:
        # The browser does `new Date(generatedAt)`; millisecond precision with a "Z" is the
        # format every JavaScript engine reads reliably.
        return v.isoformat(timespec="milliseconds").replace("+00:00", "Z")
