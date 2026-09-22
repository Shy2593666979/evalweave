from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EvaluationModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    model_name: str = Field(min_length=1, max_length=128)
    api_mode: Literal["responses", "chat_completions"] = "responses"
    api_key: str = Field(min_length=1, max_length=4096)
    is_active: bool = True


class EvaluationModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    api_mode: Literal["responses", "chat_completions"] | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    is_active: bool | None = None


class EvaluationModelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    base_url: str
    model_name: str
    api_mode: str
    is_active: bool
    api_key_configured: bool = True
    created_at: datetime
    updated_at: datetime
