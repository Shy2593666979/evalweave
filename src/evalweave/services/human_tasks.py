from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from evalweave.db.models import (
    AgentJobStatus,
    HumanTask,
    HumanTaskStatus,
    NotificationDelivery,
    User,
)
from evalweave.notifications import notify_human_task
from evalweave.services.agent_jobs import AgentJobService
from evalweave.services.exceptions import ConflictError, NotFoundError


class HumanTaskService:
    @staticmethod
    def require(session: Session, task_id: UUID) -> HumanTask:
        task = session.get(HumanTask, task_id)
        if task is None:
            raise NotFoundError("人工任务不存在")
        return task

    @staticmethod
    def list(
        session: Session,
        task_status: HumanTaskStatus | None = None,
    ) -> list[HumanTask]:
        statement = select(HumanTask)
        if task_status is not None:
            statement = statement.where(HumanTask.status == task_status)
        return list(session.exec(statement.order_by(HumanTask.created_at.desc())).all())

    @staticmethod
    def decide(
        session: Session,
        task_id: UUID,
        user: User,
        decision: str,
        reason: str | None,
    ) -> tuple[HumanTask, bool]:
        task = HumanTaskService.require(session, task_id)
        if task.status != HumanTaskStatus.PENDING:
            raise ConflictError("人工任务已经处理")
        job = AgentJobService.require(session, task.job_id)
        task.status = (
            HumanTaskStatus.APPROVED
            if decision == "approve"
            else HumanTaskStatus.REJECTED
        )
        task.decision_reason = reason
        task.resolved_by = user.id
        task.resolved_at = datetime.now(UTC)
        task.updated_at = datetime.now(UTC)
        approved = task.status == HumanTaskStatus.APPROVED
        job.status = AgentJobStatus.RUNNING if approved else AgentJobStatus.CANCELLED
        job.updated_at = datetime.now(UTC)
        session.add(task)
        session.add(job)
        session.commit()
        session.refresh(task)
        return task, approved

    @staticmethod
    def resend_notification(
        session: Session,
        task_id: UUID,
    ) -> list[dict[str, Any]]:
        task = HumanTaskService.require(session, task_id)
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

    @staticmethod
    def list_deliveries(
        session: Session,
        task_id: UUID,
    ) -> list[dict[str, Any]]:
        HumanTaskService.require(session, task_id)
        statement = (
            select(NotificationDelivery)
            .where(NotificationDelivery.human_task_id == task_id)
            .order_by(NotificationDelivery.created_at.desc())
        )
        return [
            {
                "id": str(item.id),
                "channel": item.channel,
                "recipient": item.recipient,
                "status": item.status,
                "error": item.error,
                "sent_at": item.sent_at,
            }
            for item in session.exec(statement).all()
        ]
