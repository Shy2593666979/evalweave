from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from uuid import UUID

from celery.signals import worker_ready
from sqlmodel import Session

from evalweave.core.config import get_settings, set_config_path
from evalweave.db.session import get_engine
from evalweave.workers.factory import create_celery_app

logger = logging.getLogger(__name__)


def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def run_agent_job_task(job_id: str) -> None:
    from evalweave.agents.workflow import plan_agent_job

    plan_agent_job(UUID(job_id))


def resume_agent_job_task(job_id: str) -> None:
    from evalweave.agents.workflow import execute_agent_job

    execute_agent_job(UUID(job_id))


def run_python_job_task(job_id: str) -> None:
    from evalweave.agents.workflow import execute_python_job

    execute_python_job(UUID(job_id))


def finalize_human_review_task(campaign_id: str) -> None:
    from evalweave.human_reviews import finalize_campaign

    finalize_campaign(UUID(campaign_id))


def dispatch_schedules_task() -> dict[str, int]:
    from evalweave.db.models import AgentJob
    from evalweave.services.scheduled_evaluations import (
        dispatch_due_schedules,
        mark_job_dispatch_failed,
    )

    with Session(get_engine()) as session:
        job_ids = dispatch_due_schedules(session)
        jobs = [session.get(AgentJob, job_id) for job_id in job_ids]
    dispatched = 0
    failed = 0
    for job in jobs:
        if job is None:
            continue
        task_name = (
            "evalweave.python.run"
            if job.input_config.get("job_type") == "python"
            else "evalweave.agent.execute"
        )
        try:
            create_celery_app().send_task(task_name, args=[str(job.id)])
            dispatched += 1
        except Exception as error:
            failed += 1
            logger.exception("Failed to dispatch scheduled evaluation %s", job.id)
            with Session(get_engine()) as session:
                stored = session.get(AgentJob, job.id)
                if stored is not None:
                    mark_job_dispatch_failed(session, stored, error)
    return {"dispatched": dispatched, "failed": failed}


celery_app = create_celery_app()
celery_app.task(name="evalweave.healthcheck")(healthcheck)
celery_app.task(name="evalweave.agent.plan")(run_agent_job_task)
celery_app.task(name="evalweave.agent.execute")(resume_agent_job_task)
celery_app.task(name="evalweave.python.run")(run_python_job_task)
celery_app.task(name="evalweave.human_reviews.finalize")(finalize_human_review_task)


@worker_ready.connect
def recover_interrupted_jobs(sender=None, **_kwargs) -> None:
    """Requeue jobs whose worker disappeared while a Python step was running."""
    from evalweave.agents.workflow import recover_interrupted_python_jobs

    with Session(get_engine()) as session:
        job_ids = recover_interrupted_python_jobs(session)
    application = getattr(sender, "app", None) or celery_app
    for job_id in job_ids:
        application.send_task("evalweave.python.run", args=[str(job_id)])
    if job_ids:
        logger.warning("Recovered %s interrupted Python job(s)", len(job_ids))


def worker_main() -> None:
    parser = argparse.ArgumentParser(description="Run an EvalWeave Celery worker")
    parser.add_argument("--config", type=Path, default=Path("config/application.yaml"))
    parser.add_argument("--loglevel", default="INFO")
    args = parser.parse_args()
    set_config_path(args.config)
    settings = get_settings()
    application = create_celery_app()
    application.task(name="evalweave.healthcheck")(healthcheck)
    application.task(name="evalweave.agent.plan")(run_agent_job_task)
    application.task(name="evalweave.agent.execute")(resume_agent_job_task)
    application.task(name="evalweave.python.run")(run_python_job_task)
    application.task(name="evalweave.human_reviews.finalize")(finalize_human_review_task)
    worker_args = ["worker", f"--loglevel={args.loglevel}"]
    if sys.platform == "win32":
        worker_args.extend(
            ["--pool=threads", f"--concurrency={settings.celery.worker_concurrency}"]
        )
    else:
        worker_args.append(f"--concurrency={settings.celery.worker_concurrency}")
    application.worker_main(worker_args)


def scheduler_main() -> None:
    parser = argparse.ArgumentParser(description="Run the EvalWeave schedule dispatcher")
    parser.add_argument("--config", type=Path, default=Path("config/application.yaml"))
    parser.add_argument("--loglevel", default="INFO")
    args = parser.parse_args()
    set_config_path(args.config)
    settings = get_settings()
    from evalweave.core.logging import configure_logging
    from evalweave.db.session import create_db_and_tables

    configure_logging(settings)
    create_db_and_tables()
    interval = settings.scheduler.poll_interval_seconds
    logger.info("Database scheduler started (poll interval: %ss)", interval)
    try:
        while True:
            started = time.monotonic()
            try:
                result = dispatch_schedules_task()
                if result["dispatched"]:
                    logger.info("Dispatched %s scheduled evaluation(s)", result["dispatched"])
            except Exception:
                logger.exception("Scheduled evaluation dispatch failed")
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    except KeyboardInterrupt:
        logger.info("Database scheduler stopped")
