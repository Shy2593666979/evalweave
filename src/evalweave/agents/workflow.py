from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID

from sqlmodel import Session

from evalweave.agents.executor import execute_http_target
from evalweave.agents.inspection import inspect_source
from evalweave.agents.planner import generate_eval_spec, generate_summary
from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    AgentStep,
    FileObject,
    HumanTask,
    StepStatus,
)
from evalweave.db.session import get_engine
from evalweave.notifications import notify_human_task
from evalweave.storage import LocalFileStorage

StepCallable = Callable[[], dict[str, Any]]


def update_job(session: Session, job: AgentJob, status: AgentJobStatus) -> None:
    job.status = status
    job.updated_at = datetime.now(UTC)
    session.add(job)
    session.commit()


def run_step(
    session: Session,
    job: AgentJob,
    name: str,
    operation: StepCallable,
    input_data: dict[str, Any] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    step = AgentStep(
        job_id=job.id,
        name=name,
        status=StepStatus.RUNNING,
        attempt=attempt,
        input_data=input_data or {},
        started_at=datetime.now(UTC),
    )
    session.add(step)
    session.commit()
    try:
        output = operation()
    except Exception as error:
        step.status = StepStatus.FAILED
        step.error = str(error)[:4000]
        step.finished_at = datetime.now(UTC)
        step.updated_at = datetime.now(UTC)
        session.add(step)
        session.commit()
        raise
    step.status = StepStatus.COMPLETED
    step.output_data = output
    step.finished_at = datetime.now(UTC)
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.commit()
    return output


def plan_agent_job(job_id: UUID) -> None:
    settings = get_settings()
    task_to_notify: HumanTask | None = None
    should_execute = False
    with Session(get_engine()) as session:
        job = session.get(AgentJob, job_id)
        if job is None:
            raise ValueError(f"Agent job not found: {job_id}")
        if job.status not in {AgentJobStatus.PENDING, AgentJobStatus.FAILED}:
            return
        job.error = None
        try:
            update_job(session, job, AgentJobStatus.DISCOVERING)
            discovery: dict[str, Any] = {"format": None, "fields": [], "samples": []}
            if job.source_file_id:
                file_object = session.get(FileObject, job.source_file_id)
                if file_object is None or file_object.project_id != job.project_id:
                    raise ValueError("Source file does not belong to this project")
                storage = LocalFileStorage(settings.storage.local_directory)
                path = storage.path_for(file_object.storage_key)
                discovery = run_step(
                    session,
                    job,
                    "discover_source",
                    lambda: inspect_source(
                        path, file_object.original_name, settings.agent.dry_run_cases
                    ),
                    {"source_file_id": str(file_object.id)},
                )

            update_job(session, job, AgentJobStatus.PLANNING)
            spec: dict[str, Any] | None = None
            previous_error: str | None = None
            for attempt in range(1, job.max_repair_attempts + 2):
                try:
                    spec = run_step(
                        session,
                        job,
                        "generate_eval_spec",
                        partial(
                            generate_eval_spec,
                            settings.agent,
                            job.goal,
                            discovery,
                            job.input_config,
                            previous_error,
                        ),
                        {"goal": job.goal},
                        attempt=attempt,
                    )
                    break
                except Exception as error:
                    if attempt > job.max_repair_attempts:
                        raise
                    previous_error = str(error)[:2000]
                    job.repair_attempts = attempt
                    job.updated_at = datetime.now(UTC)
                    session.add(job)
                    session.commit()
            if spec is None:
                raise RuntimeError("Agent failed to generate EvalSpec")
            job.eval_spec = spec
            job.updated_at = datetime.now(UTC)
            session.add(job)

            if job.requires_approval:
                task_to_notify = HumanTask(
                    job_id=job.id,
                    title=f"评测 Agent 等待确认：{job.title}",
                    instructions="请检查 Agent 生成的 EvalSpec，确认后平台将继续执行。",
                    notification_targets=job.input_config.get("notification_targets", []),
                )
                session.add(task_to_notify)
                job.status = AgentJobStatus.WAITING_HUMAN
            else:
                job.status = AgentJobStatus.RUNNING
                should_execute = True
            session.commit()
        except Exception as error:
            session.rollback()
            job = session.get(AgentJob, job_id)
            if job is not None:
                job.status = AgentJobStatus.FAILED
                job.error = str(error)[:4000]
                job.updated_at = datetime.now(UTC)
                session.add(job)
                session.commit()
            return

        if task_to_notify is not None:
            session.refresh(task_to_notify)
            notify_human_task(session, task_to_notify)

    if should_execute:
        execute_agent_job(job_id)


def execute_agent_job(job_id: UUID) -> None:
    with Session(get_engine()) as session:
        job = session.get(AgentJob, job_id)
        if job is None:
            raise ValueError(f"Agent job not found: {job_id}")
        if job.status in {AgentJobStatus.COMPLETED, AgentJobStatus.CANCELLED}:
            return
        try:
            update_job(session, job, AgentJobStatus.RUNNING)

            def execute_spec() -> dict[str, Any]:
                operations = job.eval_spec.get("operations", [])
                target_result = execute_http_target(session, job)
                return {
                    "execution_mode": "declarative",
                    "operations": operations,
                    "source": job.eval_spec.get("source", {}),
                    "target": target_result,
                }

            execution = run_step(session, job, "execute_eval_spec", execute_spec)
            update_job(session, job, AgentJobStatus.ANALYZING)

            def summarize_execution() -> dict[str, Any]:
                summary = generate_summary(get_settings().agent, job.goal, execution)
                summary["execution"] = execution
                return summary

            result = run_step(
                session,
                job,
                "summarize",
                summarize_execution,
            )
            job.result = result
            job.error = None
            update_job(session, job, AgentJobStatus.COMPLETED)
        except Exception as error:
            session.rollback()
            job = session.get(AgentJob, job_id)
            if job is not None:
                job.status = AgentJobStatus.FAILED
                job.error = str(error)[:4000]
                job.updated_at = datetime.now(UTC)
                session.add(job)
                session.commit()
