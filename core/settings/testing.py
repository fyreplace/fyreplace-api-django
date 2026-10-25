from . import *

IS_TESTING = True

AWS_ACCESS_KEY_ID = None

STORAGES = {
    "default": {"BACKEND": "core.storages.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

CELERY_TASK_ALWAYS_EAGER = True

CELERY_TASK_EAGER_PROPAGATES = True

APNS_PRIVATE_KEY = None

FIREBASE_APP = None

GRAVATAR_BASE_URL = None
