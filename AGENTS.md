# AGENTS.md — fyreplace-api (Django gRPC)

Django 6.1 + Python 3.14 backend for [Fyreplace](https://fyreplace.net), serving a gRPC API
defined by protobuf contracts. This is the reference implementation; a Quarkus port lives in
`../fyreplace-api-quarkus-grpc` (consult it for behavioral parity, but do not translate 1:1 —
the architectures differ).

- API contracts (`.proto` files) live in the git submodule `protos/`
  (github.com/fyreplace/fyreplace-protos). **Never edit generated `*_pb2.py` / `*_pb2_grpc.py`
  stubs.** To change an API shape: commit in the submodule first, bump the pointer here, then
  run `make protobufs` to regenerate. RPC names look like
  `fyreplace.protos.UserService/RetrieveMe`.
- The API is **gRPC-only** — there is no REST surface. A minimal HTTP health check exists at
  `/health/`.

## Commands

| Task | Command |
|---|---|
| Install dependencies | `uv sync` |
| Regenerate protobuf stubs | `make protobufs` |
| Compile MJML email templates | `make emails` |
| Run migrations | `make migrations` or `uv run python manage.py migrate` |
| Collect static files | `make static` |
| Run all tests | `uv run python manage.py test` |
| Run tests with pytest directly | `uv run pytest` |
| Run a single test module | `uv run pytest users/test_models.py` |
| Format code | `uv run black . && uv run isort .` |
| Type check | `uv run mypy` (django-stubs is configured) |
| Start gRPC server | `uv run python manage.py grpc` (see `core/grpc.py`) |
| Start Celery worker | `uv run celery -A core worker` |
| Start Celery beat | `uv run celery -A core beat` |

Requirements: Python 3.14, `uv`, Node/npx (for `make emails`). Copy `.env-example` to `.env`
and adjust — it covers database, S3, Celery/Redis, Mailgun, APNS, Firebase, and Sentry.

## Architecture map

- **Django apps** (each follows the same internal layout):
  - `core/` — shared infrastructure: settings, JWT, gRPC server/interceptors, pagination,
    base models (`UUIDModel`, `SoftDeleteModel`, `TimestampModel`, `MessageConvertible`),
    email base class, Celery app, storage backends, custom validators/lookups.
  - `users/` — `User` (custom `AbstractUser` with UUID PK, soft delete), `Connection`,
    `Block`; account lifecycle (signup, email activation, password reset), connections
    (multi-device sessions), profile management.
  - `posts/` — `Post`, `Chapter`, `Comment`, `Vote`, `Stack`, `Subscription`; the content
    feed, drafts, comments, voting, and subscription model.
  - `notifications/` — `Notification`, `Flag`, `ApnsToken`; in-app notifications and remote
    push (APNS + FCM) dispatch.
  - `tooling/` — currently empty placeholder app.

- **Per-app file layout** (follow this when adding features):
  - `models.py` — Django models; use `UUIDModel`, `SoftDeleteModel`, `TimestampModel` from
    `core.models` as appropriate. Always set `default_auto_field` in `apps.py`.
  - `services.py` — gRPC servicer implementations. Each service class extends the generated
    `*_pb2_grpc.*ServiceServicer` and usually mixes in `PaginatorMixin` and/or
    `ImageUploadMixin` from `core`.
  - `tasks.py` — Celery `@shared_task` functions. Naming convention: `send_*` → `messaging`
    queue, `remove_*` / `cleanup_*` → `trash` queue (see `CELERY_TASK_ROUTES` in settings).
  - `signals.py` — Django signal handlers; imported in `apps.py` `ready()`.
  - `pagination.py` — `PaginationAdapter` subclasses for cursor-based pagination.
  - `validators.py` — custom validators (raise `grpc_interceptor.exceptions.InvalidArgument`).
  - `emails.py` — `Email` subclasses for transactional emails (MJML templates in
    `users/templates/`).

- **gRPC server** (`core/grpc.py`): auto-discovers servicers from each app's `services.py`.
  Interceptors (in order): `ExceptionInterceptor` → `AuthorizationInterceptor` →
  `CacheInterceptor`. Auth is JWT (HS256, `SECRET_KEY`-signed) passed as
  `Authorization: Bearer <token>` metadata. Tokens carry `user_id` and either `connection_id`
  or `timestamp` (24h validity for email tokens).

- **Authentication decorator**: `@no_auth` (from `core.authentication`) marks RPCs that don't
  require a token. Without it, the `AuthorizationInterceptor` requires a valid JWT.

- **Pagination** (`core/pagination.py`): cursor-based, not offset-based. Clients send a
  `Page` stream; first message is a `Header` (forward/backward, page size, optional context_id),
  subsequent messages carry `Cursor` data. Max page size is `PAGINATION_MAX_SIZE` (50).

- **Email** (`core/emails.py` + `users/emails.py`): `Email` base class renders Django templates
  (`<name>.txt` and `<name>.html`). HTML templates are compiled from MJML (`*.html.mjml`) via
  `make emails`. In dev without Mailgun configured, emails go to the console backend.

- **Storage** (`core/storages.py`): S3-compatible via `django-storages` when
  `S3_ACCESS_KEY_ID` is set; otherwise local filesystem. Uploaded files get UUID-based names.

- **Celery** (`core/celery.py`): Redis broker. Beat schedule includes periodic cleanup tasks
  (inactive users, old connections, stale stacks, APNS token refresh). Tasks are idempotent.

- **Settings** (`core/settings/`): `__init__.py` is the base (reads from env vars);
  `testing.py` overrides for tests (in-memory storage, eager Celery, no APNS/Firebase).
  `manage.py` auto-selects `testing` when `test` is in `sys.argv`.

## Conventions

### Code style

- **black** + **isort** (profile=black). Run both before committing. Line length is black's
  default (88).
- Type hints are used throughout. `django-stubs` provides Django types for mypy.
- Prefer explicit imports over wildcard imports (except `core/jwt.py` which re-exports `jwt`
  for convenience).
- The codebase is mostly comment-free by design — rely on precise naming. Add comments only
  for non-obvious constraints or intentional deviations.

### Models

- All models use UUID primary keys (`UUIDModel`) and timestamps (`TimestampModel`:
  `date_created`, `date_updated`).
- Soft deletion via `SoftDeleteModel` (`is_deleted` flag + `date_deleted`). Use
  `existing_objects` manager to filter out soft-deleted records. Signals `pre_soft_delete` /
  `post_soft_delete` are available in `core.signals`.
- Custom user model: `users.User` (set via `AUTH_USER_MODEL`). Password hashing prefers
  Argon2.
- Queryset methods for access control: `get_readable_by`, `get_writable_by`,
  `filter_readable_by` — raise `PermissionDenied` (from `grpc_interceptor.exceptions`) on
  unauthorized access.

### Services (gRPC)

- Services live in `<app>/services.py` and extend the generated `*Servicer` base class.
- Use `@no_auth` for public RPCs. All other RPCs require a valid JWT.
- Access the authenticated user via `context.caller` (set by `store_user` in `core/grpc.py`)
  or `get_info_from_token(token)`.
- Raise gRPC exceptions from `grpc_interceptor.exceptions` (`InvalidArgument`,
  `PermissionDenied`, `Unauthenticated`, `AlreadyExists`, `NotFound`, etc.) — never raw
  Django exceptions. The `ExceptionInterceptor` maps Django exceptions to gRPC statuses.
- Convert models to protobuf messages via `MessageConvertible.to_message()`. The
  `default_message_class` class attribute on each model specifies its default proto message.
- Use `@atomic` (from `django.db.transaction`) for multi-write operations. Prefer
  `select_for_update()` when locking rows.

### Tasks (Celery)

- All background work goes through `@shared_task` in `<app>/tasks.py`.
- Email-sending tasks go to the `messaging` queue; cleanup/removal tasks go to `trash`.
- Use `autoretry_for` with `retry_backoff=True` for tasks that may hit transient failures
  (e.g., `IntegrityError`, `ObjectDoesNotExist`).
- Tasks receive primitive IDs (UUIDs as strings), not model instances — look up objects inside
  the task.

### Testing

- Tests use **pytest** with `pytest-django` and `pytest-xdist` (parallel execution via
  `--numprocesses=auto`).
- Test settings: `core.settings.testing` (auto-selected by `manage.py test`).
- Test files are named `test_*.py` (not `tests.py` — though legacy `tests.py` files exist).
- Base test classes in `core/tests.py`:
  - `BaseTestCase` — provides `FakeContext` (mock gRPC context) and `assertEmails`.
  - `PaginationTestCase` — mixin for testing cursor pagination (forward, reverse, empty,
    invalid size, out-of-bounds).
  - `ImageTestCaseMixin` — helper for image upload tests.
- Tests run against PostgreSQL in CI (GitHub Actions service container). SQLite works locally.
- Celery tasks run eagerly in tests (`CELERY_TASK_ALWAYS_EAGER = True`).
- Email assertions use Django's `mail.outbox`.

### Database

- Migrations are standard Django migrations. Run `make migrations` after model changes.
- The project supports PostgreSQL (recommended), MySQL/MariaDB, and SQLite via
  `DATABASE_URL` or individual `DATABASE_*` env vars.
- `CONN_MAX_AGE = None` and `CONN_HEALTH_CHECKS = True` — connection pooling is managed by
  Django's health checks, not persistent connections.

## Gotchas

- **Protobuf stubs are generated** — always run `make protobufs` after pulling submodule
  changes. The `protos/__init__.py` file is created by the Makefile; don't delete it.
- **MJML templates** — edit `.html.mjml` files, never the generated `.html` files. Run
  `make emails` to recompile.
- **`.env` file** — copy from `.env-example`. The settings module calls `load_dotenv()` on
  startup, so env vars are read from the file automatically.
- **Testing settings** — `core/settings/testing.py` disables S3, APNS, Firebase, and Gravatar.
  Celery runs eagerly. Don't add test-specific logic to production settings.
- **Soft delete vs hard delete** — `SoftDeleteModel.delete()` sets `is_deleted=True`. Use
  `existing_objects` manager (or `filter(is_deleted=False)`) to exclude soft-deleted records
  from queries. Some models override `delete()` for additional cleanup (e.g., wiping PII).
- **gRPC context** — `context.caller` is the authenticated `User`, `context.caller_connection`
  is the `Connection` (if the token carries `connection_id`). Both are `None` for `@no_auth`
  endpoints without a token.
- **Pagination cursors** — cursor data is an ordered list of key-value pairs. The pagination
  adapter handles forward/backward navigation. Don't implement offset-based pagination for new
  endpoints.
- **Image uploads** — the `ImageUploadMixin.get_image()` method handles chunked image uploads
  via gRPC streaming. It validates MIME type (PNG/JPEG/WebP only) and enforces
  `FILE_UPLOAD_MAX_MEMORY_SIZE` (1 MB).

## CI/CD

- **Tests** (`.github/workflows/shipping.yml`): runs on push to `develop` and version tags.
  Spins up PostgreSQL, runs `uv sync`, `make protobufs`, then `python manage.py test`.
- **CodeQL** (`.github/workflows/scanning.yml`): security scanning on push to `develop`.
- **Docker**: `Dockerfile` builds a Python 3.14 image, runs `uv sync`, and `make protobufs`.

## License

AGPLv3+ (see `COPYING.txt`). All contributions are subject to this license.
