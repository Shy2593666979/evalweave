from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SystemRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class AgentJobStatus(StrEnum):
    PENDING = "pending"
    DISCOVERING = "discovering"
    PLANNING = "planning"
    WAITING_HUMAN = "waiting_human"
    RUNNING = "running"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HumanTaskStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class ScheduledEvaluationStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ScheduleStatus(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    ARCHIVED = "archived"


class TimestampMixin(SQLModel):
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)


class UserType(TimestampMixin, table=True):
    __tablename__ = "user_types"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))
    name: str = Field(sa_column=Column(String(64), unique=True, nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text))
    permissions: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    selectable_on_registration: bool = Field(default=True, nullable=False)
    is_active: bool = Field(default=True, index=True, nullable=False)


class User(TimestampMixin, table=True):
    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    username: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))
    email: str | None = Field(
        default=None,
        sa_column=Column(String(255), unique=True, index=True, nullable=True),
    )
    password_hash: str = Field(sa_column=Column(String(255), nullable=False))
    system_role: SystemRole = Field(default=SystemRole.USER, index=True)
    user_type_id: UUID | None = Field(default=None, foreign_key="user_types.id", index=True)
    is_active: bool = Field(default=True, index=True, nullable=False)
    last_login_at: datetime | None = None


class Project(TimestampMixin, table=True):
    __tablename__ = "projects"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True, max_length=128)
    description: str | None = Field(default=None, sa_column=Column(Text))
    service_url: str | None = Field(default=None, max_length=512)
    agent_context: str | None = Field(default=None, sa_column=Column(Text))


class ProjectMember(TimestampMixin, table=True):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)


class FileObject(TimestampMixin, table=True):
    __tablename__ = "file_objects"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    created_by: UUID = Field(foreign_key="users.id", index=True)
    category: str = Field(index=True, max_length=32)
    original_name: str = Field(max_length=255)
    storage_key: str = Field(sa_column=Column(String(512), unique=True, nullable=False))
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(index=True, min_length=64, max_length=64)


class AgentJob(TimestampMixin, table=True):
    __tablename__ = "agent_jobs"
    __table_args__ = (UniqueConstraint("schedule_id", "scheduled_for"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    created_by: UUID = Field(foreign_key="users.id", index=True)
    source_file_id: UUID | None = Field(default=None, foreign_key="file_objects.id", index=True)
    result_file_id: UUID | None = Field(default=None, foreign_key="file_objects.id", index=True)
    title: str = Field(max_length=128)
    goal: str = Field(sa_column=Column(Text, nullable=False))
    status: AgentJobStatus = Field(default=AgentJobStatus.PENDING, index=True)
    input_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    eval_spec: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    error: str | None = Field(default=None, sa_column=Column(Text))
    repair_attempts: int = Field(default=0, ge=0)
    max_repair_attempts: int = Field(default=3, ge=0)
    requires_approval: bool = Field(default=False, nullable=False)
    scheduled_evaluation_id: UUID | None = Field(
        default=None,
        sa_column=Column("experiment_id", Uuid, ForeignKey("experiments.id"), index=True),
    )
    snapshot_id: UUID | None = Field(
        default=None, sa_column=Column("experiment_version_id", Uuid, index=True)
    )
    schedule_id: UUID | None = Field(default=None, index=True)
    trigger_type: str = Field(default="manual", index=True, max_length=16)
    scheduled_for: datetime | None = Field(default=None, index=True)


class EvaluationModel(TimestampMixin, table=True):
    __tablename__ = "evaluation_models"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(sa_column=Column(String(128), unique=True, nullable=False))
    base_url: str = Field(max_length=512)
    model_name: str = Field(max_length=128)
    api_mode: str = Field(default="responses", max_length=32)
    api_key_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    is_active: bool = Field(default=True, index=True, nullable=False)


class AssistantConversation(TimestampMixin, table=True):
    __tablename__ = "assistant_conversations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID | None = Field(default=None, foreign_key="projects.id", index=True)
    created_by: UUID = Field(foreign_key="users.id", index=True)
    title: str = Field(default="新的评测对话", max_length=128)
    draft: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="collecting", index=True, max_length=32)
    agent_job_id: UUID | None = Field(default=None, foreign_key="agent_jobs.id", index=True)


class AssistantMessage(TimestampMixin, table=True):
    __tablename__ = "assistant_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    conversation_id: UUID = Field(foreign_key="assistant_conversations.id", index=True)
    role: str = Field(max_length=16)
    content: str = Field(sa_column=Column(Text, nullable=False))
    ui_action: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    include_in_context: bool = Field(default=True, nullable=False)
    is_streaming: bool = Field(default=False, nullable=False)
    attachment_file_id: UUID | None = Field(default=None, foreign_key="file_objects.id", index=True)
    attachment_name: str | None = Field(default=None, max_length=255)
    attachment_content_type: str | None = Field(default=None, max_length=255)
    attachment_size_bytes: int | None = Field(default=None, ge=0)


class AgentStep(TimestampMixin, table=True):
    __tablename__ = "agent_steps"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="agent_jobs.id", index=True)
    name: str = Field(index=True, max_length=64)
    status: StepStatus = Field(default=StepStatus.PENDING, index=True)
    attempt: int = Field(default=1, ge=1)
    input_data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    output_data: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentJobEvent(TimestampMixin, table=True):
    __tablename__ = "agent_job_events"

    id: int | None = Field(default=None, primary_key=True)
    job_id: UUID = Field(foreign_key="agent_jobs.id", index=True)
    phase: str = Field(index=True, max_length=64)
    event_type: str = Field(index=True, max_length=32)
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class HumanTask(TimestampMixin, table=True):
    __tablename__ = "human_tasks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="agent_jobs.id", index=True)
    title: str = Field(max_length=128)
    instructions: str = Field(sa_column=Column(Text, nullable=False))
    status: HumanTaskStatus = Field(default=HumanTaskStatus.PENDING, index=True)
    notification_targets: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    decision_reason: str | None = Field(default=None, sa_column=Column(Text))
    resolved_by: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    resolved_at: datetime | None = None


class HumanReviewCampaign(TimestampMixin, table=True):
    __tablename__ = "human_review_campaigns"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="agent_jobs.id", index=True)
    created_by: UUID = Field(foreign_key="users.id", index=True)
    title: str = Field(max_length=128)
    instructions: str = Field(default="", sa_column=Column(Text, nullable=False))
    status: str = Field(default="active", index=True, max_length=32)
    rubric: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    blind_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    reviewer_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    reviews_per_item: int = Field(default=1, ge=1)
    item_count: int = Field(default=0, ge=0)
    total_assignments: int = Field(default=0, ge=0)
    completed_assignments: int = Field(default=0, ge=0)
    deadline_at: datetime | None = Field(default=None, index=True)
    summary_started_at: datetime | None = None
    completion_reason: str | None = Field(default=None, max_length=32)
    summary: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    completed_at: datetime | None = None


class HumanReviewItem(TimestampMixin, table=True):
    __tablename__ = "human_review_items"
    __table_args__ = (UniqueConstraint("campaign_id", "source_index"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    campaign_id: UUID = Field(foreign_key="human_review_campaigns.id", index=True)
    source_index: int = Field(ge=0)
    prompt: str = Field(default="", sa_column=Column(Text, nullable=False))
    response: str = Field(default="", sa_column=Column(Text, nullable=False))
    visible_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    private_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )


class HumanReviewAssignment(TimestampMixin, table=True):
    __tablename__ = "human_review_assignments"
    __table_args__ = (UniqueConstraint("item_id", "reviewer_id"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    campaign_id: UUID = Field(foreign_key="human_review_campaigns.id", index=True)
    item_id: UUID = Field(foreign_key="human_review_items.id", index=True)
    reviewer_id: UUID = Field(foreign_key="users.id", index=True)
    status: str = Field(default="pending", index=True, max_length=32)
    dimension_scores: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    overall_score: float | None = None
    reason: str | None = Field(default=None, sa_column=Column(Text))
    submitted_at: datetime | None = None


class NotificationDelivery(TimestampMixin, table=True):
    __tablename__ = "notification_deliveries"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    human_task_id: UUID = Field(foreign_key="human_tasks.id", index=True)
    channel: str = Field(index=True, max_length=32)
    recipient: str = Field(max_length=255)
    status: DeliveryStatus = Field(default=DeliveryStatus.PENDING, index=True)
    provider_message_id: str | None = Field(default=None, max_length=255)
    error: str | None = Field(default=None, sa_column=Column(Text))
    sent_at: datetime | None = None


class Dataset(TimestampMixin, table=True):
    __tablename__ = "datasets"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    name: str = Field(index=True, max_length=128)
    description: str | None = Field(default=None, sa_column=Column(Text))


class DatasetVersion(TimestampMixin, table=True):
    __tablename__ = "dataset_versions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    dataset_id: UUID = Field(foreign_key="datasets.id", index=True)
    version: int = Field(ge=1)
    source_filename: str | None = Field(default=None, max_length=255)
    case_count: int = Field(default=0, ge=0)


class TestCase(TimestampMixin, table=True):
    __tablename__ = "test_cases"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    dataset_version_id: UUID = Field(foreign_key="dataset_versions.id", index=True)
    external_id: str = Field(index=True, max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    expected: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    metadata_: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON))


class ScheduledEvaluation(TimestampMixin, table=True):
    # Preserve the legacy table name so existing installations keep their data.
    __tablename__ = "experiments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    created_by: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    name: str = Field(index=True, max_length=128)
    description: str | None = Field(default=None, sa_column=Column(Text))
    configuration: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: ScheduledEvaluationStatus = Field(default=ScheduledEvaluationStatus.DRAFT, index=True)
    current_snapshot_id: UUID | None = Field(
        default=None, sa_column=Column("current_version_id", Uuid, index=True)
    )


class EvaluationSnapshot(TimestampMixin, table=True):
    # Preserve legacy storage identifiers while exposing scheduling terminology.
    __tablename__ = "experiment_versions"
    __table_args__ = (UniqueConstraint("experiment_id", "version"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    scheduled_evaluation_id: UUID = Field(
        sa_column=Column(
            "experiment_id", Uuid, ForeignKey("experiments.id"), nullable=False, index=True
        )
    )
    version: int = Field(ge=1)
    source_job_id: UUID = Field(foreign_key="agent_jobs.id", index=True)
    preview_job_id: UUID = Field(foreign_key="agent_jobs.id", index=True)
    source_file_id: UUID | None = Field(default=None, foreign_key="file_objects.id", index=True)
    goal: str = Field(sa_column=Column(Text, nullable=False))
    input_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    eval_spec: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result_preview: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    published_at: datetime | None = None


class EvaluationSchedule(TimestampMixin, table=True):
    __tablename__ = "evaluation_schedules"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    scheduled_evaluation_id: UUID = Field(
        sa_column=Column(
            "experiment_id", Uuid, ForeignKey("experiments.id"), nullable=False, index=True
        )
    )
    snapshot_id: UUID = Field(
        sa_column=Column(
            "experiment_version_id",
            Uuid,
            ForeignKey("experiment_versions.id"),
            nullable=False,
            index=True,
        )
    )
    created_by: UUID = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=128)
    recurrence: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    status: ScheduleStatus = Field(default=ScheduleStatus.ENABLED, index=True)
    next_run_at: datetime = Field(index=True)
    last_run_at: datetime | None = None
    overlap_policy: str = Field(default="skip", max_length=16)
    misfire_policy: str = Field(default="latest", max_length=16)


class ExperimentRun(TimestampMixin, table=True):
    __tablename__ = "experiment_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    experiment_id: UUID = Field(foreign_key="experiments.id", index=True)
    dataset_version_id: UUID = Field(foreign_key="dataset_versions.id", index=True)
    status: RunStatus = Field(default=RunStatus.PENDING, index=True)
    total_cases: int = Field(default=0, ge=0)
    completed_cases: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CaseRun(TimestampMixin, table=True):
    __tablename__ = "case_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    experiment_run_id: UUID = Field(foreign_key="experiment_runs.id", index=True)
    test_case_id: UUID = Field(foreign_key="test_cases.id", index=True)
    status: RunStatus = Field(default=RunStatus.PENDING, index=True)
    trace_id: str | None = Field(default=None, index=True, max_length=64)
    output: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))


class EvaluationResult(TimestampMixin, table=True):
    __tablename__ = "evaluation_results"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    case_run_id: UUID = Field(foreign_key="case_runs.id", index=True)
    evaluator: str = Field(index=True, max_length=128)
    score: float | None = None
    passed: bool | None = None
    reason: str | None = Field(default=None, sa_column=Column(Text))
    evidence: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
