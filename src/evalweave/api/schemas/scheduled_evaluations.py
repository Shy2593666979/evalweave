from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from evalweave.db.models import AgentJobStatus, ScheduleStatus


class RecurrencePayload(BaseModel):
    type: Literal["daily", "weekly"]
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=list)


class ScheduledEvaluationCreate(BaseModel):
    source_job_id: UUID
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    recurrence: RecurrencePayload
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    overlap_policy: Literal["skip", "parallel"] = "skip"
    misfire_policy: Literal["latest", "skip"] = "latest"


class ScheduleEnabledUpdate(BaseModel):
    enabled: bool


class ScheduleUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    recurrence: RecurrencePayload
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    overlap_policy: Literal["skip", "parallel"] = "skip"
    misfire_policy: Literal["latest", "skip"] = "latest"


class ScheduledRunRead(BaseModel):
    id: UUID
    status: AgentJobStatus
    scheduled_for: datetime | None
    created_at: datetime
    updated_at: datetime
    error: str | None
    result_file_id: UUID | None


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scheduled_evaluation_id: UUID
    snapshot_id: UUID
    created_by: UUID
    name: str
    recurrence: dict
    timezone: str
    status: ScheduleStatus
    next_run_at: datetime
    last_run_at: datetime | None
    overlap_policy: str
    misfire_policy: str
    created_at: datetime
    updated_at: datetime


class ScheduledEvaluationRead(BaseModel):
    schedule: ScheduleRead
    name: str
    description: str | None
    source_job_id: UUID
    goal: str
    output_format: str
    recent_runs: list[ScheduledRunRead]
