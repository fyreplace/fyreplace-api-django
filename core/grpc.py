from concurrent import futures
from datetime import timedelta
from importlib import import_module
from inspect import getmembers
from typing import Any, Iterable, Iterator, Optional

import grpc
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils.timezone import now
from google.protobuf.message import Message
from grpc_interceptor.exceptions import Unauthenticated

from users.models import Connection, User

from . import jwt
from .interceptors import (
    AuthorizationInterceptor,
    CacheInterceptor,
    ExceptionInterceptor,
)
from .services import get_servicer_interfaces


def create_server() -> grpc.Server:
    services = list(all_servicers())
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.MAX_CONCURRENCY),
        interceptors=(
            ExceptionInterceptor(),
            AuthorizationInterceptor(services),
            CacheInterceptor(),
        ),
    )

    if settings.SSL_PRIVATE_KEY:
        ssl = (settings.SSL_PRIVATE_KEY, settings.SSL_CERTIFICATE_CHAIN)
        server.add_secure_port(settings.GRPC_URL, grpc.ssl_server_credentials([ssl]))
    else:
        server.add_insecure_port(settings.GRPC_URL)

    _add_services_to_server(services, server)
    return server


def store_user(context: grpc.ServicerContext) -> Optional[User]:
    if token := get_token(context):
        context.caller, context.caller_connection = get_info_from_token(token)
        return context.caller
    else:
        context.caller, context.caller_connection = None, None
        return None


def get_token(context: grpc.ServicerContext) -> Optional[str]:
    metadata = dict(context.invocation_metadata())

    if not (token := metadata.get("authorization")):
        return None

    token_parts = token.split(" ")

    if len(token_parts) != 2 or token_parts[0] != "Bearer":
        raise Unauthenticated("parse_error")

    return token_parts[1]


def get_info_from_token(
    token: str, for_update: bool = False
) -> tuple[User, Optional[Connection]]:
    try:
        claims = jwt.decode(token)
        user_objects = User.objects.select_for_update() if for_update else User.objects
        user = user_objects.get(id=claims["user_id"])
        connection = None

        if connection_id := claims.get("connection_id"):
            connection = Connection.objects.get(id=connection_id)

            if connection.user_id != user.id:
                raise Unauthenticated("user_id_connection_id_mismatch")
        elif timestamp := claims.get("timestamp"):
            deadline = now() - timedelta(days=1)

            if timestamp < deadline.timestamp():
                raise Unauthenticated("timestamp_exceeded")
        else:
            raise Unauthenticated("missing_connection_id_or_timestamp")

        return user, connection
    except KeyError, jwt.InvalidTokenError, ObjectDoesNotExist:
        raise Unauthenticated("invalid_token")


def get_request_id(context: grpc.ServicerContext) -> Optional[str]:
    return dict(context.invocation_metadata()).get("x-request-id")


def serialize_message(message: Message) -> dict:
    data = {}

    for field in message.DESCRIPTOR.fields_by_name.keys():
        data[field] = getattr(message, field)

    return data


def all_servicers() -> Iterator[type[Any]]:
    for app in apps.get_app_configs():
        try:
            services_module = import_module(app.module.__name__ + ".services")
        except ImportError, AttributeError:
            continue

        for _, entity in getmembers(services_module):
            if len(get_servicer_interfaces(entity)) > 0:
                yield entity


def _add_services_to_server(services: Iterable[type[Any]], server: grpc.Server):
    for service in services:
        servicers = get_servicer_interfaces(service)

        for servicer in servicers:
            addition = f"add_{servicer.__name__}_to_server"
            module = import_module(servicer.__module__)

            for name, entity in getmembers(module):
                if name == addition and callable(entity):
                    entity(servicer=service(), server=server)
