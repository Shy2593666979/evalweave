from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    AgentStep,
    EvaluationModel,
    FileObject,
    HumanReviewAssignment,
    HumanReviewCampaign,
    HumanTask,
    HumanTaskStatus,
    StepStatus,
    SystemRole,
    User,
)
from evalweave.services.exceptions import ConflictError, NotFoundError, ValidationError
from evalweave.services.projects import ProjectService


class AgentJobService:
    ACTIVE_STATUSES = {
        AgentJobStatus.PENDING,
        AgentJobStatus.DISCOVERING,
        AgentJobStatus.PLANNING,
        AgentJobStatus.WAITING_HUMAN,
        AgentJobStatus.RUNNING,
        AgentJobStatus.ANALYZING,
    }

    @staticmethod
    def list_evaluation_models(session: Session) -> list[EvaluationModel]:
        statement = (
            select(EvaluationModel).where(EvaluationModel.is_active).order_by(EvaluationModel.name)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def require(session: Session, job_id: UUID) -> AgentJob:
        job = session.get(AgentJob, job_id)
        if job is None:
            raise NotFoundError("评测任务不存在")
        return job

    @staticmethod
    def require_for_user(session: Session, job_id: UUID, user: User) -> AgentJob:
        job = AgentJobService.require(session, job_id)
        if user.system_role != SystemRole.ADMIN and job.created_by != user.id:
            raise NotFoundError("评测任务不存在")
        ProjectService.require_access(session, user, job.project_id)
        return job

    @staticmethod
    def create(
        session: Session,
        project_id: UUID,
        user: User,
        values: dict[str, Any],
    ) -> AgentJob:
        ProjectService.require_access(session, user, project_id)
        source_file_id = values.get("source_file_id")
        if source_file_id:
            file_object = session.get(FileObject, source_file_id)
            if file_object is None or file_object.project_id != project_id:
                raise ValidationError("源文件不属于当前项目")
        input_config = dict(values.get("input_config") or {})
        input_config["output_format"] = values.get("output_format", "xlsx")
        evaluation_model_id = values.get("evaluation_model_id")
        if evaluation_model_id:
            input_config["evaluation_model_id"] = str(evaluation_model_id)
        input_config["notification_targets"] = values.get("notification_targets") or []
        job = AgentJob(
            project_id=project_id,
            created_by=user.id,
            source_file_id=source_file_id,
            title=str(values["title"]).strip(),
            goal=str(values["goal"]).strip(),
            input_config=input_config,
            max_repair_attempts=get_settings().agent.max_repair_attempts,
            requires_approval=False,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    @staticmethod
    def list(session: Session, project_id: UUID, user: User) -> list[AgentJob]:
        ProjectService.require_access(session, user, project_id)
        statement = select(AgentJob).where(AgentJob.project_id == project_id)
        if user.system_role != SystemRole.ADMIN:
            statement = statement.where(AgentJob.created_by == user.id)
        return list(session.exec(statement.order_by(AgentJob.created_at.desc())).all())

    @staticmethod
    def list_steps(session: Session, job_id: UUID, user: User) -> list[AgentStep]:
        AgentJobService.require_for_user(session, job_id, user)
        statement = (
            select(AgentStep).where(AgentStep.job_id == job_id).order_by(AgentStep.created_at)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def restart(session: Session, job_id: UUID, user: User) -> tuple[AgentJob, str]:
        job = AgentJobService.require_for_user(session, job_id, user)
        if job.status not in {
            AgentJobStatus.PENDING,
            AgentJobStatus.FAILED,
            AgentJobStatus.COMPLETED,
            AgentJobStatus.CANCELLED,
        }:
            raise ConflictError("当前状态下无法启动评测任务")
        job.status = AgentJobStatus.PENDING
        job.error = None
        job.updated_at = datetime.now(UTC)
        is_python_job = job.input_config.get("job_type") == "python"
        if not is_python_job:
            job.eval_spec = {}
        elif not job.eval_spec:
            job.eval_spec = {"execution_mode": "agent_python"}
        job.result = {}
        job.result_file_id = None
        session.add(job)
        session.commit()
        session.refresh(job)
        task_name = "evalweave.python.run" if is_python_job else "evalweave.agent.plan"
        return job, task_name

    @staticmethod
    def cancel(session: Session, job_id: UUID, user: User) -> AgentJob:
        job = AgentJobService.require_for_user(session, job_id, user)
        if job.status == AgentJobStatus.CANCELLED:
            return job
        if job.status not in AgentJobService.ACTIVE_STATUSES:
            raise ConflictError("任务已经结束，无需停止")
        now = datetime.now(UTC)
        job.status = AgentJobStatus.CANCELLED
        job.error = None
        job.updated_at = now
        steps = session.exec(
            select(AgentStep).where(
                AgentStep.job_id == job.id,
                AgentStep.status.in_([StepStatus.PENDING, StepStatus.RUNNING]),
            )
        ).all()
        for step in steps:
            step.status = StepStatus.CANCELLED
            step.finished_at = now
            step.updated_at = now
            session.add(step)
        human_tasks = session.exec(
            select(HumanTask).where(
                HumanTask.job_id == job.id,
                HumanTask.status == HumanTaskStatus.PENDING,
            )
        ).all()
        for task in human_tasks:
            task.status = HumanTaskStatus.CANCELLED
            task.updated_at = now
            session.add(task)
        campaigns = session.exec(
            select(HumanReviewCampaign).where(
                HumanReviewCampaign.job_id == job.id,
                HumanReviewCampaign.status.in_(["active", "summarizing"]),
            )
        ).all()
        for campaign in campaigns:
            campaign.status = "cancelled"
            campaign.completion_reason = "job_cancelled"
            campaign.completed_at = now
            campaign.updated_at = now
            session.add(campaign)
            assignments = session.exec(
                select(HumanReviewAssignment).where(
                    HumanReviewAssignment.campaign_id == campaign.id,
                    HumanReviewAssignment.status == "pending",
                )
            ).all()
            for assignment in assignments:
                assignment.status = "cancelled"
                assignment.updated_at = now
                session.add(assignment)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job
