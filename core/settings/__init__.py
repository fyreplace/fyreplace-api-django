import json
import logging
import os
import re
import tempfile
from base64 import b64decode
from datetime import timedelta
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urlparse

import firebase_admin
import sentry_sdk
from dotenv import find_dotenv, load_dotenv
from firebase_admin.credentials import Certificate as FirebaseCertificate

from ..utils import str_to_bool

# Development

load_dotenv(find_dotenv())

if debug_str := os.getenv("DEBUG", "False"):
    DEBUG = str_to_bool(debug_str)
else:
    DEBUG = False

TEST_RUNNER = "core.tests.PytestTestRunner"

IS_TESTING = False

# Sentry

SENTRY_DSN = os.getenv("SENTRY_DSN")

SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT")

if SENTRY_DSN and SENTRY_ENVIRONMENT:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        send_default_pii=True,
    )

# Self-awareness

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("SECRET_KEY")

BASE_URL = os.getenv("BASE_URL")

ALLOWED_HOSTS = []

for host in os.getenv("ALLOWED_HOSTS", "").split(","):
    host = host.strip()

    if host:
        ALLOWED_HOSTS.append(host)

if len(ALLOWED_HOSTS) == 0:
    ALLOWED_HOSTS = ["*"]

APP_NAME = "fyreplace"

PRETTY_APP_NAME = APP_NAME.capitalize()

# Application definition

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_extensions",
    "anymail",
    "tooling",
    "core",
    "users",
    "posts",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

# Database


def db_engine(name: str):
    overrides = {
        "sqlite": "sqlite3",
        "mariadb": "mysql",
        "postgres": "postgresql",
    }

    return "django.db.backends." + overrides.get(name, name)


if database_url := os.getenv("DATABASE_URL"):
    parsed_url = urlparse(database_url)
    DB_ENGINE = db_engine(parsed_url.scheme or "sqlite")
    DB_NAME = parsed_url.path.removeprefix("/")
    DB_USER, DB_PASSWORD, DB_HOST, DB_PORT = (
        re.split(r"[:@]", parsed_url.netloc) + [None] * 4
    )[:4]
else:
    DB_ENGINE = db_engine(os.getenv("DATABASE_ENGINE", "sqlite"))
    DB_NAME = os.getenv("DATABASE_NAME")
    DB_USER = os.getenv("DATABASE_USER")
    DB_PASSWORD = os.getenv("DATABASE_PASSWORD")
    DB_HOST = os.getenv("DATABASE_HOST")
    DB_PORT = os.getenv("DATABASE_PORT")

if DB_ENGINE == db_engine("sqlite") and not DB_NAME:
    DB_NAME = "db.sqlite3"

DATABASES = {
    "default": {
        "ENGINE": DB_ENGINE,
        "NAME": DB_NAME,
        "USER": DB_USER,
        "PASSWORD": DB_PASSWORD,
        "HOST": DB_HOST,
        "PORT": DB_PORT,
        "CONN_MAX_AGE": None,
        "CONN_HEALTH_CHECKS": True,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Authentication

AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{validator}"}
    for validator in [
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    ]
]

PASSWORD_HASHERS = [
    f"django.contrib.auth.hashers.{hasher}"
    for hasher in [
        "Argon2PasswordHasher",
        "PBKDF2PasswordHasher",
        "PBKDF2SHA1PasswordHasher",
        "BCryptSHA256PasswordHasher",
    ]
]

# JSON Web Tokens

JWT_ALGORITHM = "HS256"

# Internationalization

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

# Static and media files

AWS_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")

AWS_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")

AWS_DEFAULT_ACL = "public-read"

AWS_QUERYSTRING_AUTH = False

AWS_STORAGE_BUCKET_NAME = os.getenv("S3_STORAGE_BUCKET_NAME")

AWS_S3_REGION_NAME = os.getenv("S3_REGION_NAME")

AWS_S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")

AWS_S3_CUSTOM_DOMAIN = os.getenv("S3_CUSTOM_DOMAIN")

AWS_S3_ADDRESSING_STYLE = "virtual"

STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR / "static"

MEDIA_URL = "/media/"

MEDIA_ROOT = BASE_DIR / "media"

FILE_UPLOAD_MAX_MEMORY_SIZE = 1 * 1024 * 1024

STORAGES = {
    "default": {
        "BACKEND": (
            "storages.backends.s3boto3.S3Boto3Storage"
            if AWS_ACCESS_KEY_ID
            else "core.storages.FileSystemStorage"
        )
    },
    "staticfiles": {
        "BACKEND": (
            "storages.backends.s3boto3.S3StaticStorage"
            if AWS_ACCESS_KEY_ID
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

VALID_IMAGE_MIMES = [f"image/{i}" for i in ("png", "jpeg", "webp")]

# Emails

ANYMAIL = {
    "MAILGUN_WEBHOOK_SIGNING_KEY": os.getenv("MAILGUN_WEBHOOK_SIGNING_KEY"),
}

mailgun_api_url = os.getenv("MAILGUN_API_URL")
mailgun_api_key = os.getenv("MAILGUN_API_KEY")

MAILERS = {
    "default": {
        "BACKEND": (
            "anymail.backends.mailgun.EmailBackend"
            if mailgun_api_key
            else "django.core.mail.backends.console.EmailBackend"
        ),
        "OPTIONS": (
            {"api_url": mailgun_api_url, "api_key": mailgun_api_key}
            if mailgun_api_key
            else {}
        ),
    },
}

DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL")

SERVER_EMAIL = os.getenv("SERVER_EMAIL")

ADMINS = [
    (
        formataddr(tuple(part.strip() for part in admin.split(":", 1)))
        if ":" in admin
        else admin.strip()
    )
    for admin in os.getenv("ADMINS", "").split(",")
    if admin
]

EMAIL_LINKS_DOMAIN = os.getenv("EMAIL_LINKS_DOMAIN")

# gRPC

GRPC_HOST = os.getenv("GRPC_HOST", "[::]")

GRPC_PORT = os.getenv("GRPC_PORT", "50051")

GRPC_URL = f"{GRPC_HOST}:{GRPC_PORT}"

if path := os.getenv("SSL_PRIVATE_KEY_PATH"):
    with open(path, "rb") as file:
        SSL_PRIVATE_KEY = file.read()
else:
    SSL_PRIVATE_KEY = None

if path := os.getenv("SSL_CERTIFICATE_PATH"):
    with open(path, "rb") as file:
        SSL_CERTIFICATE_CHAIN = file.read()
else:
    SSL_CERTIFICATE_CHAIN = None

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", os.cpu_count()))

# Celery

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL")

CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND")

CELERY_IGNORE_RESULT = True

CELERY_TASK_ROUTES = {
    "*.send_*": "messaging",
    "*.remove_*": "trash",
    "*.cleanup_*": "trash",
}

# Beat schedule file - use OS temp dir for containers (ephemeral, but tasks are idempotent)
CELERY_BEAT_SCHEDULE_FILENAME = os.getenv(
    "CELERY_BEAT_SCHEDULE_FILENAME",
    str(Path(tempfile.gettempdir()) / "celerybeat-schedule"),
)

# Apple Push Notification Serivce

APPLE_TEAM_ID = os.getenv("APPLE_TEAM_ID")

APPLE_APP_ID = os.getenv("APPLE_APP_ID")

APNS_URL = os.getenv("APNS_URL")

APNS_PRIVATE_KEY_ID = os.getenv("APNS_PRIVATE_KEY_ID")

if path := os.getenv("APNS_PRIVATE_KEY_PATH"):
    with open(path, "rb") as file:
        APNS_PRIVATE_KEY = file.read()
else:
    if file_b64 := os.getenv("APNS_PRIVATE_KEY_B64"):
        APNS_PRIVATE_KEY = b64decode(file_b64)
    else:
        APNS_PRIVATE_KEY = None

# Firebase

try:
    FIREBASE_APP = firebase_admin.get_app()
except ValueError:
    if path := os.getenv("FIREBASE_ACCOUNT_PATH"):
        credential = path
    elif file_b64 := os.getenv("FIREBASE_ACCOUNT_B64"):
        credential = json.loads(b64decode(file_b64))
    else:
        credential = None

    FIREBASE_APP = (
        firebase_admin.initialize_app(FirebaseCertificate(credential))
        if credential
        else None
    )

# Fyreplace

FYREPLACE_INACTIVE_USER_DURATION = timedelta(days=1)

FYREPLACE_CONNECTION_DURATION = timedelta(weeks=4)

FYREPLACE_POST_SPREAD_LIFE = 3

FYREPLACE_POST_MAX_DURATION = timedelta(weeks=1)

# Other

PAGINATION_MAX_SIZE = 50

GRAVATAR_BASE_URL = "https://www.gravatar.com"
