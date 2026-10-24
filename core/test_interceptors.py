from typing import Iterator

import grpc
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from google.protobuf import empty_pb2
from grpc_interceptor.exceptions import InvalidArgument, Unauthenticated

from protos import user_pb2
from users.tests import AuthenticatedTestCase, UserContext

from .grpc import all_servicers
from .interceptors import (
    AuthorizationInterceptor,
    CacheInterceptor,
    ExceptionInterceptor,
    make_method_name,
)
from .models import CachedRequest
from .tests import BaseTestCase, FakeContext


class MakeMethodName(BaseTestCase):
    def test(self):
        self.assertEqual(
            make_method_name("fyreplace.protos", "UserService", "Retrieve"),
            "/fyreplace.protos.UserService/Retrieve",
        )


class ExceptionInterceptorTestCase(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.interceptor = ExceptionInterceptor()

    def intercept(self, behavior, request=None):
        return self.interceptor.intercept(
            lambda request, context: behavior(),
            request or empty_pb2.Empty(),
            self.grpc_context,
            "/fyreplace.protos.TestService/Test",
        )

    def test(self):
        self.assertEqual(self.intercept(lambda: "ok"), "ok")

    def test_permission_denied(self):
        with self.assertRaises(PermissionDenied):
            self.intercept(lambda: (_ for _ in ()).throw(PermissionDenied("no")))

        self.assertEqual(self.grpc_context._code, grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(self.grpc_context._details, "no")

    def test_validation_error(self):
        with self.assertRaises(ValidationError):
            self.intercept(lambda: (_ for _ in ()).throw(ValidationError("bad")))

        self.assertEqual(self.grpc_context._code, grpc.StatusCode.INVALID_ARGUMENT)
        self.assertEqual(self.grpc_context._details, str(ValidationError("bad")))

    def test_object_does_not_exist(self):
        exception = ObjectDoesNotExist("missing")

        with self.assertRaises(ObjectDoesNotExist):
            self.intercept(lambda: (_ for _ in ()).throw(exception))

        self.assertEqual(self.grpc_context._code, grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.grpc_context._details, "missing")

    def test_grpc_exception(self):
        with self.assertRaises(InvalidArgument):
            self.intercept(lambda: (_ for _ in ()).throw(InvalidArgument("invalid")))

        self.assertEqual(self.grpc_context._code, grpc.StatusCode.INVALID_ARGUMENT)
        self.assertEqual(self.grpc_context._details, "invalid")

    def test_unknown_exception(self):
        with self.assertRaises(RuntimeError):
            self.intercept(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        self.assertEqual(self.grpc_context._code, grpc.StatusCode.UNKNOWN)
        self.assertEqual(self.grpc_context._details, "boom")

    def test_generator(self):
        def behavior() -> Iterator[int]:
            yield 1
            yield 2

        generator = self.intercept(behavior)
        self.assertEqual(list(generator), [1, 2])

    def test_generator_error(self):
        def behavior() -> Iterator[int]:
            yield 1
            raise PermissionDenied("no")

        generator = self.intercept(behavior)
        self.assertEqual(next(generator), 1)

        with self.assertRaises(PermissionDenied):
            next(generator)

        self.assertEqual(self.grpc_context._code, grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(self.grpc_context._details, "no")


class AuthorizationInterceptorTestCase(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.interceptor = AuthorizationInterceptor(list(all_servicers()))

    def intercept(self, context, method_name):
        return self.interceptor.intercept(
            lambda request, context: "ok",
            empty_pb2.Empty(),
            context,
            method_name,
        )

    def test_no_auth_methods(self):
        self.assertEqual(
            set(self.interceptor.no_auth_method_names),
            {
                "/fyreplace.protos.AccountService/Create",
                "/fyreplace.protos.AccountService/SendActivationEmail",
                "/fyreplace.protos.AccountService/ConfirmActivation",
                "/fyreplace.protos.AccountService/SendConnectionEmail",
                "/fyreplace.protos.AccountService/ConfirmConnection",
                "/fyreplace.protos.AccountService/Connect",
                "/fyreplace.protos.UserService/Retrieve",
                "/fyreplace.protos.PostService/ListFeed",
                "/fyreplace.protos.CommentService/List",
            },
        )

    def test_with_user(self):
        self.assertEqual(
            self.intercept(
                self.grpc_context, "/fyreplace.protos.UserService/RetrieveMe"
            ),
            "ok",
        )

    def test_without_user(self):
        context = UserContext()

        with self.assertRaises(Unauthenticated):
            self.intercept(context, "/fyreplace.protos.UserService/RetrieveMe")

    def test_without_user_on_no_auth_method(self):
        context = UserContext()
        self.assertEqual(
            self.intercept(context, "/fyreplace.protos.UserService/Retrieve"), "ok"
        )


class CacheInterceptorTestCase(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.interceptor = CacheInterceptor()

    def intercept(self, method_name="/fyreplace.protos.TestService/Test"):
        return self.interceptor.intercept(
            lambda request, context: user_pb2.Token(token="abc"),
            empty_pb2.Empty(),
            self.grpc_context,
            method_name,
        )

    def test(self):
        self.grpc_context._invocation_metadata["x-request-id"] = "42"
        message = self.intercept()
        self.assertEqual(message, user_pb2.Token(token="abc"))
        self.assertEqual(CachedRequest.objects.count(), 1)

        cached = self.intercept()
        self.assertEqual(cached, message)
        self.assertEqual(CachedRequest.objects.count(), 1)

    def test_no_request_id(self):
        self.intercept()
        self.assertEqual(CachedRequest.objects.count(), 0)

    def test_different_request_ids(self):
        self.grpc_context._invocation_metadata["x-request-id"] = "42"
        self.intercept()
        self.grpc_context._invocation_metadata["x-request-id"] = "43"
        self.intercept()
        self.assertEqual(CachedRequest.objects.count(), 2)

    def test_non_message_response(self):
        self.grpc_context._invocation_metadata["x-request-id"] = "42"
        self.interceptor.intercept(
            lambda request, context: "not a message",
            empty_pb2.Empty(),
            self.grpc_context,
            "/fyreplace.protos.TestService/Test",
        )
        self.assertEqual(CachedRequest.objects.count(), 0)
