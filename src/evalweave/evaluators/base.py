from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class EvaluationContext(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)


class EvaluationOutcome(BaseModel):
    evaluator: str
    score: float | None = Field(default=None, ge=0, le=1)
    passed: bool | None = None
    reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class Evaluator(ABC):
    name: str

    @abstractmethod
    async def evaluate(self, context: EvaluationContext) -> EvaluationOutcome:
        raise NotImplementedError
