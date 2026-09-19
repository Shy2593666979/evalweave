from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from evalweave.api.response import APIResponse
from evalweave.api.schemas.agents import AgentJobRead
from evalweave.api.schemas.scheduled_evaluations import (
    ScheduledEvaluationCreate,
    ScheduledEvaluationRead,
    ScheduleEnabledUpdate,
    ScheduleRead,
    ScheduleUpdate,
)
from evalweave.auth.dependencies import require_permission
from evalweave.auth.permissions import Permission
from evalweave.db.models import AgentJob, EvaluationSchedule, User
from evalweave.db.session import get_session
from evalweave.services.scheduled_evaluations import (
    ScheduledEvaluationService,
    mark_job_dispatch_failed,
)
from evalweave.workers.factory import create_celery_app

router = APIRouter(tags=["scheduled-evaluations"])

SessionDependency = Annotated[Session, Depends(get_session)]
ScheduleReader = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_READ))]
ScheduleRunner = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_RUN))]


def enqueue_scheduled_job(job: AgentJob) -> None:
    task_name = (
        "evalweave.python.run"
        if job.input_config.get("job_type") == "python"
        else "evalweave.agent.execute"
    )
    create_celery_app().send_task(task_name, args=[str(job.id)])


@router.get(
    "/projects/{project_id}/scheduled-evaluations",
    response_model=APIResponse[list[ScheduledEvaluationRead]],
)
def list_project_scheduled_evaluations(
    project_id: UUID, user: ScheduleReader, session: SessionDependency
) -> APIResponse[list[dict]]:
    return APIResponse.success(
        ScheduledEvaluationService.list_project_scheduled_evaluations(
            session, project_id, user
        )
    )


@router.post(
    "/projects/{project_id}/scheduled-evaluations",
    response_model=APIResponse[ScheduledEvaluationRead],
    status_code=status.HTTP_201_CREATED,
)
def create_scheduled_evaluation(
    project_id: UUID,
    payload: ScheduledEvaluationCreate,
    user: ScheduleRunner,
    session: SessionDependency,
) -> APIResponse[dict]:
    return APIResponse.success(
        ScheduledEvaluationService.create_scheduled_evaluation(
            session, project_id, user, payload.model_dump()
        )
    )


@router.patch("/schedules/{schedule_id}/enabled", response_model=APIResponse[ScheduleRead])
def set_schedule_enabled(
    schedule_id: UUID,
    payload: ScheduleEnabledUpdate,
    user: ScheduleRunner,
    session: SessionDependency,
) -> APIResponse[EvaluationSchedule]:
    return APIResponse.success(
        ScheduledEvaluationService.set_schedule_enabled(
            session, schedule_id, user, payload.enabled
        )
    )


@router.put("/schedules/{schedule_id}", response_model=APIResponse[ScheduleRead])
def update_schedule(
    schedule_id: UUID,
    payload: ScheduleUpdate,
    user: ScheduleRunner,
    session: SessionDependency,
) -> APIResponse[EvaluationSchedule]:
    values = payload.model_dump()
    values["recurrence"] = payload.recurrence.model_dump()
    return APIResponse.success(
        ScheduledEvaluationService.update_schedule(session, schedule_id, user, values)
    )


@router.post("/schedules/{schedule_id}/run-now", response_model=APIResponse[AgentJobRead])
def run_schedule_now(
    schedule_id: UUID,
    user: ScheduleRunner,
    session: SessionDependency,
) -> APIResponse[AgentJob]:
    job = ScheduledEvaluationService.create_run_now(session, schedule_id, user)
    try:
        enqueue_scheduled_job(job)
    except Exception as error:
        mark_job_dispatch_failed(session, job, error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="评测任务派发失败，请稍后重试",
        ) from error
    return APIResponse.success(job)


@router.delete("/schedules/{schedule_id}", response_model=APIResponse[None])
def archive_schedule(
    schedule_id: UUID,
    user: ScheduleRunner,
    session: SessionDependency,
) -> APIResponse[None]:
    ScheduledEvaluationService.archive_schedule(session, schedule_id, user)
    return APIResponse.success(message="定时评测已删除")
