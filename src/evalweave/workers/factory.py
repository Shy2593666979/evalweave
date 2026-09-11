from celery import Celery

from evalweave.core.config import get_settings


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
