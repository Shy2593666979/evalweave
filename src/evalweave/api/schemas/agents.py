from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from evalweave.db.models import AgentJobStatus, HumanTaskStatus


class NotificationTarget(BaseModel):
    channel: Literal["wecom", "email"]
    recipient: str = Field(min_length=1, max_length=255)


class AgentJobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=10000)
    source_file_id: UUID | None = None
    input_config: dict[str, Any] = Field(default_factory=dict)
    notification_targets: list[NotificationTarget] = Field(default_factory=list)
    requires_approval: bool | None = None
    output_format: Literal["xlsx", "jsonl", "markdown", "text"] = "xlsx"
    evaluation_model_id: UUID | None = None


class AgentJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_by: UUID
    source_file_id: UUID | None
    result_file_id: UUID | None
    title: str
    goal: str
    status: AgentJobStatus
    input_config: dict[str, Any]
    eval_spec: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    repair_attempts: int
    max_repair_attempts: int
    requires_approval: bool
    scheduled_evaluation_id: UUID | None
    snapshot_id: UUID | None
    schedule_id: UUID | None
    trigger_type: str
    scheduled_for: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: str
    attempt: int
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class AgentJobEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: UUID
    phase: str
    event_type: str
    content: str
    payload: dict[str, Any]
    created_at: datetime


class HumanTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    title: str
    instructions: str
    status: HumanTaskStatus
    notification_targets: list[dict[str, str]]
    decision_reason: str | None
    resolved_by: UUID | None
    resolved_at: datetime | None
    created_at: datetime


class HumanDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=4000)


class AgentRuntimeRead(BaseModel):
    enabled: bool
    model: str | None
    require_approval: bool
    worker_available: bool


class AgentAssistMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=10000)


class AgentAssistRequest(BaseModel):
    messages: list[AgentAssistMessage] = Field(min_length=1, max_length=20)
    project_id: UUID | None = None
    evaluation_model_id: UUID | None = None


class AgentAssistRead(BaseModel):
    reply: str
    draft: dict[str, Any]


class AssistantConversationCreate(BaseModel):
    project_id: UUID | None = None


class AssistantConversationUpdate(BaseModel):
    title: str = Field(min_length=2, max_length=10)


class AssistantConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID | None
    title: str
    draft: dict[str, Any]
    status: str
    agent_job_id: UUID | None
    created_at: datetime
    updated_at: datetime


class AssistantMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    ui_action: dict[str, Any] | None
    include_in_context: bool
    is_streaming: bool
    attachment_file_id: UUID | None
    attachment_name: str | None
    attachment_content_type: str | None
    attachment_size_bytes: int | None
    created_at: datetime


class AssistantStreamRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    source_file_id: UUID | None = None
    evaluation_model_id: UUID | None = None
    output_format: Literal["xlsx", "jsonl", "markdown", "text"] | None = None


class AssistantStartedRequest(BaseModel):
    agent_job_id: UUID


class EvaluationModelOption(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    model_name: str
    api_mode: str
