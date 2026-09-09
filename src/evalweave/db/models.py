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
