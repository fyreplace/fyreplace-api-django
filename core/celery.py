import logging
import os
from typing import Any, Dict, List

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery(settings.APP_NAME)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "core.cleanup_cached_requests": {
        "task": "core.tasks.cleanup_cached_requests",
        "schedule": crontab(),
    },
    "users.cleanup_users": {
        "task": "users.tasks.cleanup_users",
        "schedule": crontab(minute=0),
    },
    "users.cleanup_connections": {
        "task": "users.tasks.cleanup_connections",
        "schedule": crontab(hour=0, minute=0),
    },
    "posts.cleanup_stacks": {
        "task": "posts.tasks.cleanup_stacks",
        "schedule": crontab(minute=0),
    },
    "notifications.refresh_apns_token": {
        "task": "notifications.tasks.refresh_apns_token",
        "schedule": crontab(minute="0,30"),
    },
}


@task_failure.connect
def on_task_failure(sender: Any, args: List, kwargs: Dict, **__):
    logging.error(
        msg=f"Task failed: {sender}",
        exc_info=True,
        extra={
            "arguments": args,
            "keyword_arguments": kwargs,
        },
    )
