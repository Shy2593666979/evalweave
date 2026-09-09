from __future__ import annotations

import argparse
from pathlib import Path

from celery import Celery

from evalweave.core.config import get_settings, set_config_path


def create_celery_app() -> Celery:
    settings = get_settings()
    application = Celery(
        "evalweave",
        broker=settings.celery.broker_url,
        backend=settings.celery.result_backend,
    )
    application.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone=settings.celery.timezone,
        task_time_limit=settings.celery.task_time_limit,
        task_track_started=True,
    )
    return application


def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


celery_app = create_celery_app()
celery_app.task(name="evalweave.healthcheck")(healthcheck)


def worker_main() -> None:
    parser = argparse.ArgumentParser(description="Run an EvalWeave Celery worker")
    parser.add_argument("--config", type=Path, default=Path("config/application.yaml"))
    parser.add_argument("--loglevel", default="INFO")
    args = parser.parse_args()
    set_config_path(args.config)
    settings = get_settings()
    application = create_celery_app()
    application.task(name="evalweave.healthcheck")(healthcheck)
    application.worker_main(
        [
            "worker",
            f"--loglevel={args.loglevel}",
            f"--concurrency={settings.celery.worker_concurrency}",
        ]
    )
