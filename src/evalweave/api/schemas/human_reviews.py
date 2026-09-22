from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


def default_rubric() -> list[dict[str, Any]]:
    return [
        {"key": "effect", "label": "回复效果", "min_score": 1, "max_score": 10},
        {"key": "speed", "label": "回复速度", "min_score": 1, "max_score": 10},
    ]


class ReviewItemCreate(BaseModel):
    prompt: str = Field(default="", max_length=100000)
    response: str = Field(min_length=1, max_length=200000)
    visible_metadata: dict[str, Any] = Field(default_factory=dict)
    private_metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewCampaignCreate(BaseModel):
    job_id: UUID
    title: str = Field(min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=10000)
    reviewer_ids: list[UUID] = Field(min_length=1)
    reviews_per_item: int = Field(default=1, ge=1, le=1000)
    rubric: list[dict[str, Any]] = Field(default_factory=default_rubric, min_length=1)
    blind_config: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    items: list[ReviewItemCreate] = Field(min_length=1, max_length=10000)
    deadline_hours: float | None = Field(default=None, gt=0, le=24 * 30)


class ReviewCampaignFromFileCreate(BaseModel):
    job_id: UUID
    source_file_id: UUID | None = None
    title: str = Field(min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=10000)
    reviewer_type_codes: list[str] = Field(default_factory=list)
    reviewer_usernames: list[str] = Field(default_factory=list)
    query_column: str = Field(default="query", min_length=1, max_length=128)
    answer_column: str = Field(default="answer", min_length=1, max_length=128)
    latency_column: str | None = Field(default=None, max_length=128)
    max_items: int = Field(default=10000, ge=1, le=10000)
    deadline_hours: float = Field(default=24, gt=0, le=24 * 30)
    rubric: list[dict[str, Any]] = Field(default_factory=default_rubric, min_length=1)
    blind_config: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})


class DimensionScore(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    score: float


class ReviewSubmission(BaseModel):
    dimension_scores: list[DimensionScore] = Field(min_length=1)
    overall_score: float | None = None
    reason: str | None = Field(default=None, max_length=10000)
