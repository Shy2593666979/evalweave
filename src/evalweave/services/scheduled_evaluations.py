from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    EvaluationSchedule,
    EvaluationSnapshot,
    ScheduledEvaluation,
    ScheduledEvaluationStatus,
    ScheduleStatus,
    SystemRole,
    User,
)
from evalweave.services.agent_jobs import AgentJobService
from evalweave.services.exceptions import ConflictError, NotFoundError, ValidationError
from evalweave.services.projects import ProjectService

ACTIVE_JOB_STATUSES = {
    AgentJobStatus.PENDING,
    AgentJobStatus.DISCOVERING,
    AgentJobStatus.PLANNING,
    AgentJobStatus.WAITING_HUMAN,
    AgentJobStatus.RUNNING,
    AgentJobStatus.ANALYZING,
}
MISFIRE_GRACE = timedelta(minutes=1)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_run_time(recurrence: dict[str, Any], timezone: str, after: datetime) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValidationError("无效的时区") from error
    kind = str(recurrence.get("type", ""))
    raw_time = str(recurrence.get("time", ""))
    try:
        hour, minute = (int(part) for part in raw_time.split(":"))
        run_time = time(hour=hour, minute=minute)
    except (TypeError, ValueError) as error:
        raise ValidationError("执行时间必须使用 HH:mm 格式") from error
    if kind not in {"daily", "weekly"}:
        raise ValidationError("定时类型必须是 daily 或 weekly")
    weekdays = recurrence.get("weekdays", [])
    if kind == "weekly":
        if not isinstance(weekdays, list) or not weekdays:
            raise ValidationError("每周任务至少选择一天")
        if any(not isinstance(day, int) or day < 0 or day > 6 for day in weekdays):
            raise ValidationError("星期取值必须在 0 到 6 之间")
    allowed_days = set(weekdays) if kind == "weekly" else set(range(7))
    local_after = _aware_utc(after).astimezone(zone)
    for offset in range(0, 15):
        candidate_date: date = local_after.date() + timedelta(days=offset)
        if candidate_date.weekday() not in allowed_days:
            continue
        candidate = datetime.combine(candidate_date, run_time, tzinfo=zone)
        if candidate > local_after:
            return candidate.astimezone(UTC)
    raise ValidationError("无法计算下一次执行时间")


class ScheduledEvaluationService:
    @staticmethod
    def require(
        session: Session, scheduled_evaluation_id: UUID, user: User
    ) -> ScheduledEvaluation:
        scheduled_evaluation = session.get(ScheduledEvaluation, scheduled_evaluation_id)
        if scheduled_evaluation is None:
            raise NotFoundError("评测方案不存在")
        ProjectService.require_access(session, user, scheduled_evaluation.project_id)
        if (
            user.system_role != SystemRole.ADMIN
            and scheduled_evaluation.created_by != user.id
        ):
            raise NotFoundError("评测方案不存在")
        return scheduled_evaluation

    @staticmethod
    def create_from_job(
        session: Session,
        project_id: UUID,
        job_id: UUID,
        user: User,
        name: str,
        description: str | None,
        draft_schedule: dict[str, Any] | None = None,
    ) -> ScheduledEvaluation:
        job = AgentJobService.require_for_user(session, job_id, user)
        if job.project_id != project_id:
            raise ValidationError("评测任务不属于当前项目")
        if job.status != AgentJobStatus.COMPLETED:
            raise ConflictError("评测完成并看到结果后，才能保存为定时方案")
        if not job.eval_spec:
            raise ValidationError("当前任务没有可复用的评测方案")
        if draft_schedule:
            next_run_time(
                draft_schedule["recurrence"],
                str(draft_schedule.get("timezone") or "Asia/Shanghai"),
                datetime.now(UTC),
            )
        configuration: dict[str, Any] = {
            "source": "agent_job",
            "source_job_id": str(job.id),
        }
        if draft_schedule:
            configuration["draft_schedule"] = draft_schedule
        scheduled_evaluation = ScheduledEvaluation(
            project_id=job.project_id,
            created_by=user.id,
            name=name.strip(),
            description=(description or "").strip() or None,
            configuration=configuration,
        )
        session.add(scheduled_evaluation)
        session.flush()
        snapshot = EvaluationSnapshot(
            scheduled_evaluation_id=scheduled_evaluation.id,
            version=1,
            source_job_id=job.id,
            preview_job_id=job.id,
            source_file_id=job.source_file_id,
            goal=job.goal,
            input_config=dict(job.input_config),
            eval_spec=dict(job.eval_spec),
            result_preview={
                "job_id": str(job.id),
                "result_file_id": str(job.result_file_id) if job.result_file_id else None,
                "result": job.result,
            },
        )
        session.add(snapshot)
        session.flush()
        scheduled_evaluation.current_snapshot_id = snapshot.id
        session.add(scheduled_evaluation)
        session.commit()
        session.refresh(scheduled_evaluation)
        return scheduled_evaluation

    @staticmethod
    def list(
        session: Session, project_id: UUID, user: User
    ) -> list[ScheduledEvaluation]:
        ProjectService.require_access(session, user, project_id)
        statement = select(ScheduledEvaluation).where(
            ScheduledEvaluation.project_id == project_id
        )
        if user.system_role != SystemRole.ADMIN:
            statement = statement.where(ScheduledEvaluation.created_by == user.id)
        return list(
            session.exec(statement.order_by(ScheduledEvaluation.updated_at.desc())).all()
        )

    @staticmethod
    def current_snapshot(
        session: Session, scheduled_evaluation: ScheduledEvaluation
    ) -> EvaluationSnapshot:
        snapshot = (
            session.get(EvaluationSnapshot, scheduled_evaluation.current_snapshot_id)
            if scheduled_evaluation.current_snapshot_id
            else None
        )
        if snapshot is None:
            raise NotFoundError("评测方案版本不存在")
        return snapshot

    @staticmethod
    def publish(
        session: Session, scheduled_evaluation_id: UUID, user: User
    ) -> ScheduledEvaluation:
        scheduled_evaluation = ScheduledEvaluationService.require(
            session, scheduled_evaluation_id, user
        )
        snapshot = ScheduledEvaluationService.current_snapshot(session, scheduled_evaluation)
        preview = session.get(AgentJob, snapshot.preview_job_id)
        if preview is None or preview.status != AgentJobStatus.COMPLETED:
            raise ConflictError("必须先完成结果预览，才能发布方案")
        now = datetime.now(UTC)
        snapshot.published_at = now
        snapshot.updated_at = now
        scheduled_evaluation.status = ScheduledEvaluationStatus.PUBLISHED
        scheduled_evaluation.updated_at = now
        draft_schedule = scheduled_evaluation.configuration.get("draft_schedule")
        if isinstance(draft_schedule, dict):
            existing_schedule = session.exec(
                select(EvaluationSchedule).where(
                    EvaluationSchedule.scheduled_evaluation_id == scheduled_evaluation.id,
                    EvaluationSchedule.snapshot_id == snapshot.id,
                )
            ).first()
            if existing_schedule is None:
                recurrence = dict(draft_schedule.get("recurrence") or {})
                timezone = str(draft_schedule.get("timezone") or "Asia/Shanghai")
                session.add(
                    EvaluationSchedule(
                        scheduled_evaluation_id=scheduled_evaluation.id,
                        snapshot_id=snapshot.id,
                        created_by=user.id,
                        name=str(
                            draft_schedule.get("name") or scheduled_evaluation.name
                        )[:128],
                        recurrence=recurrence,
                        timezone=timezone,
                        next_run_at=next_run_time(recurrence, timezone, now),
                        overlap_policy=str(draft_schedule.get("overlap_policy") or "skip"),
                        misfire_policy=str(draft_schedule.get("misfire_policy") or "latest"),
                    )
                )
        session.add(snapshot)
        session.add(scheduled_evaluation)
        session.commit()
        session.refresh(scheduled_evaluation)
        return scheduled_evaluation

    @staticmethod
    def list_schedules(
        session: Session, scheduled_evaluation_id: UUID, user: User
    ) -> list[EvaluationSchedule]:
        ScheduledEvaluationService.require(session, scheduled_evaluation_id, user)
        return list(
            session.exec(
                select(EvaluationSchedule)
                .where(
                    EvaluationSchedule.scheduled_evaluation_id
                    == scheduled_evaluation_id
                )
                .order_by(EvaluationSchedule.created_at.desc())
            ).all()
        )

    @staticmethod
    def create_schedule(
        session: Session,
        scheduled_evaluation_id: UUID,
        user: User,
        values: dict[str, Any],
    ) -> EvaluationSchedule:
        scheduled_evaluation = ScheduledEvaluationService.require(
            session, scheduled_evaluation_id, user
        )
        if scheduled_evaluation.status != ScheduledEvaluationStatus.PUBLISHED:
            raise ConflictError("方案发布后才能开启定时")
        snapshot = ScheduledEvaluationService.current_snapshot(
            session, scheduled_evaluation
        )
        now = datetime.now(UTC)
        recurrence = dict(values["recurrence"])
        timezone = str(values.get("timezone") or "Asia/Shanghai")
        schedule = EvaluationSchedule(
            scheduled_evaluation_id=scheduled_evaluation.id,
            snapshot_id=snapshot.id,
            created_by=user.id,
            name=str(values["name"]).strip(),
            recurrence=recurrence,
            timezone=timezone,
            next_run_at=next_run_time(recurrence, timezone, now),
            overlap_policy=str(values.get("overlap_policy") or "skip"),
            misfire_policy=str(values.get("misfire_policy") or "latest"),
        )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return schedule

    @staticmethod
    def set_schedule_enabled(
        session: Session, schedule_id: UUID, user: User, enabled: bool
    ) -> EvaluationSchedule:
        schedule = session.get(EvaluationSchedule, schedule_id)
        if schedule is None or schedule.status == ScheduleStatus.ARCHIVED:
            raise NotFoundError("定时规则不存在")
        ScheduledEvaluationService.require(
            session, schedule.scheduled_evaluation_id, user
        )
        schedule.status = ScheduleStatus.ENABLED if enabled else ScheduleStatus.PAUSED
        schedule.updated_at = datetime.now(UTC)
        if enabled:
            schedule.next_run_at = next_run_time(
                schedule.recurrence, schedule.timezone, datetime.now(UTC)
            )
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return schedule

    @staticmethod
    def scheduled_evaluation_detail(
        session: Session, schedule: EvaluationSchedule, user: User
    ) -> dict[str, Any]:
        scheduled_evaluation = ScheduledEvaluationService.require(
            session, schedule.scheduled_evaluation_id, user
        )
        snapshot = session.get(EvaluationSnapshot, schedule.snapshot_id)
        if snapshot is None:
            raise NotFoundError("评测方案版本不存在")
        recent_runs = list(
            session.exec(
                select(AgentJob)
                .where(AgentJob.schedule_id == schedule.id)
                .order_by(AgentJob.created_at.desc())
                .limit(10)
            ).all()
        )
        return {
            "schedule": schedule,
            "name": scheduled_evaluation.name,
            "description": scheduled_evaluation.description,
            "source_job_id": snapshot.source_job_id,
            "goal": snapshot.goal,
            "output_format": str(snapshot.input_config.get("output_format") or "file"),
            "recent_runs": recent_runs,
        }

    @staticmethod
    def list_project_scheduled_evaluations(
        session: Session, project_id: UUID, user: User
    ) -> list[dict[str, Any]]:
        scheduled_evaluations = ScheduledEvaluationService.list(session, project_id, user)
        scheduled_evaluation_ids = [item.id for item in scheduled_evaluations]
        if not scheduled_evaluation_ids:
            return []
        schedules = list(
            session.exec(
                select(EvaluationSchedule)
                .where(
                    EvaluationSchedule.scheduled_evaluation_id.in_(
                        scheduled_evaluation_ids
                    ),
                    EvaluationSchedule.status != ScheduleStatus.ARCHIVED,
                )
                .order_by(EvaluationSchedule.updated_at.desc())
            ).all()
        )
        return [
            ScheduledEvaluationService.scheduled_evaluation_detail(session, schedule, user)
            for schedule in schedules
        ]

    @staticmethod
    def create_scheduled_evaluation(
        session: Session,
        project_id: UUID,
        user: User,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        draft_schedule = {
            "name": values["name"],
            "recurrence": values["recurrence"],
            "timezone": values.get("timezone") or "Asia/Shanghai",
            "overlap_policy": values.get("overlap_policy") or "skip",
            "misfire_policy": values.get("misfire_policy") or "latest",
        }
        scheduled_evaluation = ScheduledEvaluationService.create_from_job(
            session,
            project_id,
            values["source_job_id"],
            user,
            values["name"],
            values.get("description"),
            draft_schedule,
        )
        scheduled_evaluation = ScheduledEvaluationService.publish(
            session, scheduled_evaluation.id, user
        )
        snapshot = ScheduledEvaluationService.current_snapshot(
            session, scheduled_evaluation
        )
        schedule = session.exec(
            select(EvaluationSchedule).where(
                EvaluationSchedule.scheduled_evaluation_id == scheduled_evaluation.id,
                EvaluationSchedule.snapshot_id == snapshot.id,
            )
        ).one()
        return ScheduledEvaluationService.scheduled_evaluation_detail(
            session, schedule, user
        )

    @staticmethod
    def update_schedule(
        session: Session,
        schedule_id: UUID,
        user: User,
        values: dict[str, Any],
    ) -> EvaluationSchedule:
        schedule = session.get(EvaluationSchedule, schedule_id)
        if schedule is None or schedule.status == ScheduleStatus.ARCHIVED:
            raise NotFoundError("定时规则不存在")
        ScheduledEvaluationService.require(
            session, schedule.scheduled_evaluation_id, user
        )
        now = datetime.now(UTC)
        recurrence = dict(values["recurrence"])
        timezone = str(values.get("timezone") or "Asia/Shanghai")
        schedule.name = str(values["name"]).strip()
        schedule.recurrence = recurrence
        schedule.timezone = timezone
        schedule.overlap_policy = str(values.get("overlap_policy") or "skip")
        schedule.misfire_policy = str(values.get("misfire_policy") or "latest")
        schedule.next_run_at = next_run_time(recurrence, timezone, now)
        schedule.updated_at = now
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return schedule

    @staticmethod
    def archive_schedule(
        session: Session, schedule_id: UUID, user: User
    ) -> None:
        schedule = session.get(EvaluationSchedule, schedule_id)
        if schedule is None or schedule.status == ScheduleStatus.ARCHIVED:
            raise NotFoundError("定时规则不存在")
        ScheduledEvaluationService.require(
            session, schedule.scheduled_evaluation_id, user
        )
        schedule.status = ScheduleStatus.ARCHIVED
        schedule.updated_at = datetime.now(UTC)
        session.add(schedule)
        session.commit()

    @staticmethod
    def create_run_now(
        session: Session, schedule_id: UUID, user: User
    ) -> AgentJob:
        schedule = session.get(EvaluationSchedule, schedule_id)
        if schedule is None or schedule.status == ScheduleStatus.ARCHIVED:
            raise NotFoundError("定时规则不存在")
        ScheduledEvaluationService.require(
            session, schedule.scheduled_evaluation_id, user
        )
        return create_scheduled_job(session, schedule, datetime.now(UTC), "立即运行")


def create_scheduled_job(
    session: Session,
    schedule: EvaluationSchedule,
    scheduled_for: datetime,
    title_suffix: str = "定时运行",
) -> AgentJob:
    scheduled_evaluation = session.get(
        ScheduledEvaluation, schedule.scheduled_evaluation_id
    )
    snapshot = session.get(EvaluationSnapshot, schedule.snapshot_id)
    source = session.get(AgentJob, snapshot.source_job_id) if snapshot else None
    if (
        scheduled_evaluation is None
        or scheduled_evaluation.status != ScheduledEvaluationStatus.PUBLISHED
        or snapshot is None
        or source is None
    ):
        raise ConflictError("定时评测引用的方案已不可用")
    input_config = dict(snapshot.input_config)
    input_config.pop("conversation_id", None)
    job = AgentJob(
        project_id=scheduled_evaluation.project_id,
        created_by=schedule.created_by,
        source_file_id=snapshot.source_file_id,
        title=f"{scheduled_evaluation.name} · {title_suffix}"[:128],
        goal=snapshot.goal,
        input_config=input_config,
        eval_spec=dict(snapshot.eval_spec),
        max_repair_attempts=source.max_repair_attempts,
        requires_approval=False,
        scheduled_evaluation_id=scheduled_evaluation.id,
        snapshot_id=snapshot.id,
        schedule_id=schedule.id,
        trigger_type="scheduled",
        scheduled_for=_aware_utc(scheduled_for),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def mark_job_dispatch_failed(session: Session, job: AgentJob, error: Exception) -> None:
    job.status = AgentJobStatus.FAILED
    job.error = f"定时评测派发失败：{error}"
    job.updated_at = datetime.now(UTC)
    session.add(job)
    session.commit()


def dispatch_due_schedules(session: Session, now: datetime | None = None) -> list[UUID]:
    current = _aware_utc(now or datetime.now(UTC))
    due_ids = session.exec(
        select(EvaluationSchedule.id).where(
            EvaluationSchedule.status == ScheduleStatus.ENABLED,
            EvaluationSchedule.next_run_at <= current,
        )
    ).all()
    job_ids: list[UUID] = []
    for schedule_id in due_ids:
        schedule = session.exec(
            select(EvaluationSchedule)
            .where(
                EvaluationSchedule.id == schedule_id,
                EvaluationSchedule.status == ScheduleStatus.ENABLED,
                EvaluationSchedule.next_run_at <= current,
            )
            .with_for_update(skip_locked=True)
        ).first()
        if schedule is None:
            continue
        scheduled_for = _aware_utc(schedule.next_run_at)
        if (
            schedule.misfire_policy == "skip"
            and current - scheduled_for > MISFIRE_GRACE
        ):
            schedule.next_run_at = next_run_time(
                schedule.recurrence, schedule.timezone, current
            )
            schedule.updated_at = current
            session.add(schedule)
            session.commit()
            continue
        scheduled_evaluation = session.get(
            ScheduledEvaluation, schedule.scheduled_evaluation_id
        )
        snapshot = session.get(EvaluationSnapshot, schedule.snapshot_id)
        source = session.get(AgentJob, snapshot.source_job_id) if snapshot else None
        if (
            scheduled_evaluation is None
            or scheduled_evaluation.status != ScheduledEvaluationStatus.PUBLISHED
            or snapshot is None
            or source is None
        ):
            schedule.status = ScheduleStatus.PAUSED
            schedule.updated_at = current
            session.add(schedule)
            continue
        active = session.exec(
            select(AgentJob).where(
                AgentJob.schedule_id == schedule.id,
                AgentJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        ).first()
        created = not (active and schedule.overlap_policy == "skip")
        if created:
            input_config = dict(snapshot.input_config)
            # Scheduled runs must not append their result to the original assistant chat.
            input_config.pop("conversation_id", None)
            job = AgentJob(
                project_id=scheduled_evaluation.project_id,
                created_by=schedule.created_by,
                source_file_id=snapshot.source_file_id,
                title=f"{scheduled_evaluation.name} · 定时运行"[:128],
                goal=snapshot.goal,
                input_config=input_config,
                eval_spec=dict(snapshot.eval_spec),
                max_repair_attempts=source.max_repair_attempts,
                requires_approval=False,
                scheduled_evaluation_id=scheduled_evaluation.id,
                snapshot_id=snapshot.id,
                schedule_id=schedule.id,
                trigger_type="scheduled",
                scheduled_for=scheduled_for,
            )
            session.add(job)
            try:
                session.flush()
                job_ids.append(job.id)
            except IntegrityError:
                session.rollback()
                schedule = session.get(EvaluationSchedule, schedule.id)
                if schedule is None:
                    continue
                created = False
        if created:
            schedule.last_run_at = scheduled_for
        schedule.next_run_at = next_run_time(schedule.recurrence, schedule.timezone, current)
        schedule.updated_at = current
        session.add(schedule)
        session.commit()
    return job_ids
