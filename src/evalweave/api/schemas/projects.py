from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    service_url: str | None = Field(default=None, max_length=512)
    agent_context: str | None = Field(default=None, max_length=8000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    service_url: str | None = Field(default=None, max_length=512)
    agent_context: str | None = Field(default=None, max_length=8000)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    service_url: str | None
    agent_context: str | None
    created_at: datetime
    updated_at: datetime


class ProjectMembersUpdate(BaseModel):
    user_ids: list[UUID] = Field(default_factory=list)
