import uuid
from datetime import timedelta
from time import time

from django.utils.timezone import now
from grpc_interceptor.exceptions import Unauthenticated

from protos import user_pb2
from users.models import Connection
from users.tests import AuthenticatedTestCase, BaseUserTestCase, UserContext, make_email

from . import jwt
from .grpc import (
    all_servicers,
    get_info_from_token,
    get_request_id,
    get_token,
    serialize_message,
    store_user,
)
from .tests import BaseTestCase, FakeContext


def make_timestamp_token(user_id: str, timestamp: float) -> str:
    return jwt.encode({"user_id": user_id, "timestamp": timestamp})


class GetToken(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.context = FakeContext()

    def test_no_metadata(self):
        self.assertIsNone(get_token(self.context))

    def test_no_authorization(self):
        self.context._invocation_metadata["other"] = "value"
        self.assertIsNone(get_token(self.context))

    def test_empty_authorization(self):
        self.context._invocation_metadata["authorization"] = ""
        self.assertIsNone(get_token(self.context))

    def test(self):
        self.context._invocation_metadata["authorization"] = "Bearer token"
        self.assertEqual(get_token(self.context), "token")

    def test_parse_error_no_bearer(self):
        self.context._invocation_metadata["authorization"] = "token"

        with self.assertRaises(Unauthenticated):
            get_token(self.context)

    def test_parse_error_too_many_parts(self):
        self.context._invocation_metadata["authorization"] = "Bearer a b"

        with self.assertRaises(Unauthenticated):
            get_token(self.context)

    def test_parse_error_wrong_scheme(self):
        self.context._invocation_metadata["authorization"] = "bearer token"

        with self.assertRaises(Unauthenticated):
            get_token(self.context)


class GetInfoFromToken(BaseUserTestCase):
    def test_connection_token(self):
        connection = Connection.objects.create(user=self.main_user)
        token = connection.get_token()
        user, found_connection = get_info_from_token(token)
        self.assertEqual(user.id, self.main_user.id)
        self.assertEqual(found_connection.id, connection.id)

    def test_timestamp_token(self):
        token = make_timestamp_token(str(self.main_user.id), time())
        user, connection = get_info_from_token(token)
        self.assertEqual(user.id, self.main_user.id)
        self.assertIsNone(connection)

    def test_expired_token(self):
        timestamp = (now() - timedelta(days=2)).timestamp()
        token = make_timestamp_token(str(self.main_user.id), timestamp)

        with self.assertRaises(Unauthenticated):
            get_info_from_token(token)

    def test_missing_connection_id_or_timestamp(self):
        token = jwt.encode({"user_id": str(self.main_user.id)})

        with self.assertRaises(Unauthenticated):
            get_info_from_token(token)

    def test_user_id_connection_id_mismatch(self):
        connection = Connection.objects.create(user=self.main_user)
        token = jwt.encode(
            {
                "user_id": str(self.other_user.id),
                "connection_id": str(connection.id),
            }
        )

        with self.assertRaises(Unauthenticated):
            get_info_from_token(token)

    def test_non_existent_user(self):
        token = make_timestamp_token(str(uuid.uuid4()), time())

        with self.assertRaises(Unauthenticated):
            get_info_from_token(token)

    def test_missing_user_id(self):
        token = jwt.encode({"timestamp": time()})

        with self.assertRaises(Unauthenticated):
            get_info_from_token(token)

    def test_garbage(self):
        with self.assertRaises(Unauthenticated):
            get_info_from_token("garbage")

    def test_for_update(self):
        user, _ = get_info_from_token(
            make_timestamp_token(str(self.main_user.id), time()), for_update=True
        )
        self.assertEqual(user.id, self.main_user.id)


class StoreUser(AuthenticatedTestCase):
    def test(self):
        token = self.main_connection.get_token()
        self.grpc_context._invocation_metadata["authorization"] = f"Bearer {token}"
        user = store_user(self.grpc_context)
        self.assertEqual(user.id, self.main_user.id)
        self.assertEqual(self.grpc_context.caller.id, self.main_user.id)
        self.assertEqual(
            self.grpc_context.caller_connection.id, self.main_connection.id
        )

    def test_no_token(self):
        context = UserContext()
        self.assertIsNone(store_user(context))
        self.assertIsNone(context.caller)
        self.assertIsNone(context.caller_connection)


class GetRequestId(BaseTestCase):
    def test(self):
        self.grpc_context._invocation_metadata["x-request-id"] = "42"
        self.assertEqual(get_request_id(self.grpc_context), "42")

    def test_missing(self):
        self.assertIsNone(get_request_id(self.grpc_context))


class SerializeMessage(BaseTestCase):
    def test(self):
        message = user_pb2.User(bio="Bio", email=make_email("main"))
        data = serialize_message(message)
        self.assertEqual(data["bio"], "Bio")
        self.assertEqual(data["email"], make_email("main"))
        self.assertEqual(len(data), len(user_pb2.User.DESCRIPTOR.fields))


class AllServicers(BaseTestCase):
    def test(self):
        from notifications.services import NotificationService
        from posts.services import ChapterService, CommentService, PostService
        from users.services import AccountService, UserService

        self.assertEqual(
            set(all_servicers()),
            {
                AccountService,
                UserService,
                PostService,
                ChapterService,
                CommentService,
                NotificationService,
            },
        )
