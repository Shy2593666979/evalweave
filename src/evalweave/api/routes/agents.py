from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from evalweave.auth.dependencies import SessionDependency, require_permission
from evalweave.auth.permissions import Permission
from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    AgentStep,
    FileObject,
    HumanTask,
    HumanTaskStatus,
    NotificationDelivery,
    Project,
    User,
)
from evalweave.notifications import notify_human_task
from evalweave.workers.factory import create_celery_app

router = APIRouter(tags=["evaluation-agent"])

ExperimentReader = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_READ))]
ExperimentRunner = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_RUN))]
EvaluationReviewer = Annotated[User, Depends(require_permission(Permission.EVALUATION_REVIEW))]


def enqueue_agent_task(task_name: str, job_id: UUID) -> None:
    create_celery_app().send_task(task_name, args=[str(job_id)])


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


def require_project(project_id: UUID, session: SessionDependency) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def require_job(job_id: UUID, session: SessionDependency) -> AgentJob:
    job = session.get(AgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")
    return job


def require_human_task(task_id: UUID, session: SessionDependency) -> HumanTask:
    task = session.get(HumanTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Human task not found")
    return task


@router.post(
    "/projects/{project_id}/agent-jobs",
    response_model=AgentJobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_job(
    project_id: UUID,
    payload: AgentJobCreate,
    user: ExperimentRunner,
    session: SessionDependency,
) -> AgentJob:
    require_project(project_id, session)
    if payload.source_file_id:
        file_object = session.get(FileObject, payload.source_file_id)
        if file_object is None or file_object.project_id != project_id:
            raise HTTPException(status_code=422, detail="Source file does not belong to project")
    input_config = dict(payload.input_config)
    input_config["notification_targets"] = [
        target.model_dump() for target in payload.notification_targets
    ]
    agent_config = get_settings().agent
    job = AgentJob(
        project_id=project_id,
        created_by=user.id,
        source_file_id=payload.source_file_id,
        title=payload.title.strip(),
        goal=payload.goal.strip(),
        input_config=input_config,
        max_repair_attempts=agent_config.max_repair_attempts,
        requires_approval=(
            agent_config.require_approval
            if payload.requires_approval is None
            else payload.requires_approval
        ),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/projects/{project_id}/agent-jobs", response_model=list[AgentJobRead])
def list_agent_jobs(
    project_id: UUID, _: ExperimentReader, session: SessionDependency
) -> list[AgentJob]:
    require_project(project_id, session)
    statement = select(AgentJob).where(AgentJob.project_id == project_id)
    return list(session.exec(statement.order_by(AgentJob.created_at.desc())).all())


@router.get("/agent-jobs/{job_id}", response_model=AgentJobRead)
def get_agent_job(job_id: UUID, _: ExperimentReader, session: SessionDependency) -> AgentJob:
    return require_job(job_id, session)


@router.get("/agent-jobs/{job_id}/steps", response_model=list[AgentStepRead])
def list_agent_steps(
    job_id: UUID, _: ExperimentReader, session: SessionDependency
) -> list[AgentStep]:
    require_job(job_id, session)
    statement = select(AgentStep).where(AgentStep.job_id == job_id)
    return list(session.exec(statement.order_by(AgentStep.created_at)).all())


@router.post("/agent-jobs/{job_id}/start", response_model=AgentJobRead)
def start_agent_job(
    job_id: UUID, _: ExperimentRunner, session: SessionDependency
) -> AgentJob:
    job = require_job(job_id, session)
    if job.status not in {AgentJobStatus.PENDING, AgentJobStatus.FAILED}:
        raise HTTPException(status_code=409, detail="Agent job cannot be started in current state")
    job.status = AgentJobStatus.PENDING
    job.error = None
    session.add(job)
    session.commit()
    enqueue_agent_task("evalweave.agent.plan", job.id)
    session.refresh(job)
    return job


@router.get("/human-tasks", response_model=list[HumanTaskRead])
def list_human_tasks(
    _: EvaluationReviewer,
    session: SessionDependency,
    task_status: HumanTaskStatus | None = None,
) -> list[HumanTask]:
    statement = select(HumanTask)
    if task_status is not None:
        statement = statement.where(HumanTask.status == task_status)
    return list(session.exec(statement.order_by(HumanTask.created_at.desc())).all())


@router.get("/human-tasks/{task_id}", response_model=HumanTaskRead)
def get_human_task(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> HumanTask:
    return require_human_task(task_id, session)


@router.post("/human-tasks/{task_id}/decision", response_model=HumanTaskRead)
def decide_human_task(
    task_id: UUID,
    payload: HumanDecision,
    user: EvaluationReviewer,
    session: SessionDependency,
) -> HumanTask:
    task = require_human_task(task_id, session)
    if task.status != HumanTaskStatus.PENDING:
        raise HTTPException(status_code=409, detail="Human task has already been resolved")
    job = require_job(task.job_id, session)
    task.status = (
        HumanTaskStatus.APPROVED
        if payload.decision == "approve"
        else HumanTaskStatus.REJECTED
    )
    task.decision_reason = payload.reason
    task.resolved_by = user.id
    task.resolved_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    if task.status == HumanTaskStatus.APPROVED:
        job.status = AgentJobStatus.RUNNING
    else:
        job.status = AgentJobStatus.CANCELLED
    job.updated_at = datetime.now(UTC)
    session.add(task)
    session.add(job)
    session.commit()
    if task.status == HumanTaskStatus.APPROVED:
        enqueue_agent_task("evalweave.agent.execute", job.id)
    session.refresh(task)
    return task


@router.post("/human-tasks/{task_id}/notify", response_model=list[dict[str, Any]])
def resend_human_task_notification(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> list[dict[str, Any]]:
    task = require_human_task(task_id, session)
    deliveries = notify_human_task(session, task)
    return [
        {
            "id": str(item.id),
            "channel": item.channel,
            "recipient": item.recipient,
            "status": item.status,
            "error": item.error,
        }
        for item in deliveries
    ]


@router.get("/human-tasks/{task_id}/deliveries", response_model=list[dict[str, Any]])
def list_notification_deliveries(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> list[dict[str, Any]]:
    require_human_task(task_id, session)
    statement = select(NotificationDelivery).where(NotificationDelivery.human_task_id == task_id)
    deliveries = session.exec(statement.order_by(NotificationDelivery.created_at.desc())).all()
    return [
        {
            "id": str(item.id),
            "channel": item.channel,
            "recipient": item.recipient,
            "status": item.status,
            "error": item.error,
            "sent_at": item.sent_at,
        }
        for item in deliveries
    ]
