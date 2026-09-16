from __future__ import annotations

import argparse
import logging
import sys
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
