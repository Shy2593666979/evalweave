from __future__ import annotations

import argparse
from pathlib import Path
from uuid import UUID

from evalweave.core.config import get_settings, set_config_path
from evalweave.workers.factory import create_celery_app


def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def run_agent_job_task(job_id: str) -> None:
    from evalweave.agents.workflow import plan_agent_job

    plan_agent_job(UUID(job_id))


def resume_agent_job_task(job_id: str) -> None:
    from evalweave.agents.workflow import execute_agent_job

    execute_agent_job(UUID(job_id))


celery_app = create_celery_app()
celery_app.task(name="evalweave.healthcheck")(healthcheck)
celery_app.task(name="evalweave.agent.plan")(run_agent_job_task)
celery_app.task(name="evalweave.agent.execute")(resume_agent_job_task)


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
    application.worker_main(
        [
            "worker",
            f"--loglevel={args.loglevel}",
            f"--concurrency={settings.celery.worker_concurrency}",
        ]
    )
