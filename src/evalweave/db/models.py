from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, String, Text
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


class HumanTaskStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


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
    requires_approval: bool = Field(default=True, nullable=False)


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


class Experiment(TimestampMixin, table=True):
    __tablename__ = "experiments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    name: str = Field(index=True, max_length=128)
    description: str | None = Field(default=None, sa_column=Column(Text))
    configuration: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


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
