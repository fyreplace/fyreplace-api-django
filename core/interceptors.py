import logging
import pickle
from importlib import import_module
from inspect import getmembers
from types import GeneratorType
from typing import Any, Callable, Generator

import grpc
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.utils import DataError
from google.protobuf.json_format import MessageToJson, Parse
from google.protobuf.message import Message
from grpc_interceptor.exceptions import GrpcException, Unauthenticated
from grpc_interceptor.server import ServerInterceptor

from .models import CachedRequest
from .services import get_servicer_interfaces

logging.getLogger("grpc").setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)


def make_method_name(package_name: str, service_name: str, method_name: str) -> str:
    return f"/{package_name}.{service_name}/{method_name}"


class ExceptionInterceptor(ServerInterceptor):
    def intercept(
        self,
        method: Callable,
        request: Message,
        context: grpc.ServicerContext,
        method_name: str,
    ) -> Any:
        super_intercept = super().intercept

        def action():
            return super_intercept(method, request, context, method_name)

        result = self._catch_errors(action, request, context, method_name)
        return (
            self._wrap_generator(result, request, context, method_name)
            if isinstance(result, GeneratorType)
            else result
        )

    def _wrap_generator(
        self,
        generator: Generator,
        request: Message,
        context: grpc.ServicerContext,
        method_name: str,
    ) -> Generator:
        def action():
            return next(generator)

        while True:
            try:
                item = self._catch_errors(action, request, context, method_name)
            except StopIteration:
                return

            if not item:
                return

            yield item

    def _catch_errors(
        self,
        action: Callable,
        request: Message,
        context: grpc.ServicerContext,
        method_name: str,
    ) -> Any:
        try:
            return action()
        except StopIteration, grpc.RpcError:
            raise
        except PermissionDenied as e:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(str(e))
            self._report(request, context, method_name, level=logging.WARNING)
            raise
        except (ValidationError, DataError) as e:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(e))
            self._report(request, context, method_name, level=logging.INFO)
            raise
        except ObjectDoesNotExist as e:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(e))
            self._report(request, context, method_name, level=logging.INFO)
            raise
        except GrpcException as e:
            context.set_code(e.status_code)
            context.set_details(e.details)

            if e.status_code in (
                grpc.StatusCode.OK,
                grpc.StatusCode.CANCELLED,
                grpc.StatusCode.INVALID_ARGUMENT,
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.ALREADY_EXISTS,
                grpc.StatusCode.ABORTED,
                grpc.StatusCode.UNAUTHENTICATED,
            ):
                level = logging.INFO
            elif e.status_code == grpc.StatusCode.PERMISSION_DENIED:
                level = logging.WARNING
            else:
                level = logging.ERROR

            self._report(request, context, method_name, level=level)
            raise
        except Exception as e:
            context.set_code(grpc.StatusCode.UNKNOWN)
            context.set_details(str(e))
            self._report(request, context, method_name, level=logging.CRITICAL)
            raise

    def _report(
        self,
        request: Message,
        context: grpc.ServicerContext,
        method_name: str,
        level: int,
    ):
        logger.log(
            level=level,
            msg=method_name,
            exc_info=True,
            extra={
                "request": request,
                "context_metadata": context.invocation_metadata(),
                "user_id": (
                    str(context.caller.id) if getattr(context, "caller", None) else None
                ),
            },
        )


class AuthorizationInterceptor(ServerInterceptor):
    def __init__(self, services: list[type[Any]]):
        self.no_auth_method_names = []

        for service in services:
            for servicer in get_servicer_interfaces(service):
                service_name = servicer.__name__[: -len("Servicer")]
                module_name = servicer.__module__.replace("pb2_grpc", "pb2")
                module = import_module(module_name)
                package_name = module.DESCRIPTOR.package

                for member_name in [
                    name for name, _ in getmembers(servicer) if hasattr(service, name)
                ]:
                    member = getattr(service, member_name)

                    if "no_auth" in getattr(member, "__dict__", {}):
                        name = make_method_name(package_name, service_name, member_name)
                        self.no_auth_method_names.append(name)

    def intercept(
        self,
        method: Callable,
        request: Message,
        context: grpc.ServicerContext,
        method_name: str,
    ) -> Any:
        from .grpc import store_user

        user = store_user(context)

        if not user and method_name not in self.no_auth_method_names:
            raise Unauthenticated("missing_credentials")

        return super().intercept(method, request, context, method_name)


class CacheInterceptor(ServerInterceptor):
    def intercept(
        self,
        method: Callable,
        request: Message,
        context: grpc.ServicerContext,
        method_name: str,
    ) -> Any:
        from .grpc import get_request_id

        request_id = get_request_id(context)

        if cached_request := CachedRequest.objects.filter(
            request_id=request_id
        ).first():
            message = pickle.loads(cached_request.serialized_response_message)()
            return Parse(cached_request.serialized_response, message)

        message = super().intercept(method, request, context, method_name)

        if request_id and isinstance(message, Message):
            CachedRequest.objects.create(
                request_id=request_id,
                serialized_response=MessageToJson(message),
                serialized_response_message=pickle.dumps(type(message)),
            )

        return message
