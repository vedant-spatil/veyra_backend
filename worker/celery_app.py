from celery import Celery

from app.settings import get_settings

settings = get_settings()
celery_app = Celery(
    "veyra",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    beat_schedule={
        "sync-provider-billing": {
            "task": "worker.tasks.sync_provider_billing",
            "schedule": 3600.0,
        }
    },
)
