from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

FieldType = Literal["string", "number", "boolean", "date", "url"]


class FieldSpec(BaseModel):
    name: str = Field(description="snake_case column name")
    description: str = Field(description="what this field means, so an extractor knows what to look for")
    type: FieldType = "string"
    required: bool = True


class DataSpec(BaseModel):
    """The structured plan the intent parser derives from a natural-language prompt."""

    entity: str = Field(description="short name of the thing each row represents, e.g. 'hackathon sponsor company'")
    summary: str = Field(description="one sentence restating what the user wants, for display in the UI")
    fields: List[FieldSpec]
    filters: List[str] = Field(default_factory=list, description="hard constraints to apply, e.g. 'located in India'")
    search_queries: List[str] = Field(description="3-6 distinct web search queries likely to surface source pages")
    target_count: int = Field(default=20, ge=1, le=200)
