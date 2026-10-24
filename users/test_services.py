import uuid
from datetime import datetime, timedelta
from typing import Iterator

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.images import ImageFile
from django.db.utils import DataError
from django.utils.timezone import get_current_timezone, now
from grpc_interceptor.exceptions import (
    AlreadyExists,
    Cancelled,
    InvalidArgument,
    PermissionDenied,
    Unauthenticated,
)

from core import jwt
from core.storages import get_image_url
from core.tests import ImageTestCaseMixin, PaginationTestCase, get_asset
from notifications.models import Flag, Notification
from notifications.tests import BaseNotificationTestCase
from protos import id_pb2, pagination_pb2, user_pb2

from .emails import (
    AccountActivationEmail,
    AccountConnectionEmail,
    UserBannedEmail,
    UserEmailUpdateEmail,
)
from .models import Connection, Hardware, Software, User
from .services import AccountService, UserService, check_email, check_user, normalize
from .tests import AuthenticatedTestCase, BaseUserTestCase, make_email


class AccountServiceTestCase(BaseUserTestCase):
    def setUp(self):
        super().setUp()
        self.service = AccountService()

    def _make_create_request(self, **kwargs):
        return user_pb2.UserCreation(email=make_email("new"), username="new")


class UserServiceTestCase(AuthenticatedTestCase, BaseNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.service = UserService()

    def _create_users(self, count: int) -> list[AbstractUser]:
        return [
            get_user_model().objects.create_user(
                username=f"user {i}", email=make_email(f"user-{i}")
            )
            for i in range(count)
        ]


class AccountService_Create(AccountServiceTestCase):
    def setUp(self):
        super().setUp()
        self.user_count = get_user_model().objects.count()
        self.request = self._make_create_request()

    def test(self):
        self.service.Create(self.request, self.grpc_context)
        user: AbstractUser = get_user_model().objects.latest("date_joined")
        self.assertEqual(get_user_model().objects.count(), self.user_count + 1)
        self.assertEmails([AccountActivationEmail(user.id)])
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_username_too_short(self):
        self.request.username = "a"

        with self.assertRaises(ValidationError):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_username_too_long(self):
        self.request.username = "a" * (get_user_model().username.field.max_length + 1)

        with self.assertRaises((ValidationError, DataError)):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_username_already_taken(self):
        self.request.username = self.main_user.username

        with self.assertRaises(AlreadyExists):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_username_reserved(self):
        self.request.username = "admin"

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_bad_username(self):
        self.request.username = "bad username"

        with self.assertRaises(InvalidArgument):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_bad_email(self):
        self.request.email = "bad"

        with self.assertRaises(InvalidArgument):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_email_already_taken(self):
        self.request.email = self.main_user.email

        with self.assertRaises(AlreadyExists):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_empty_username(self):
        self.request.username = ""

        with self.assertRaises(InvalidArgument):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_empty_email(self):
        self.request.email = ""

        with self.assertRaises(InvalidArgument):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_username_reserved_unicode(self):
        self.request.username = "Ádmin"

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)

    def test_username_reserved_separators(self):
        self.request.username = "ad.min"

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(get_user_model().objects.count(), self.user_count)


class AccountService_Delete(AccountServiceTestCase, AuthenticatedTestCase):
    def test(self):
        self.service.Delete(self.request, self.grpc_context)
        self.main_user.refresh_from_db()
        self.assertIsNone(self.main_user.username)
        self.assertIsNone(self.main_user.email)
        self.assertFalse(self.main_user.is_active)
        self.assertTrue(self.main_user.is_deleted)
        self.assertFalse(self.main_user.avatar)
        self.assertEqual(self.main_user.bio, "")
        self.assertEqual(self.main_user.blocked_users.count(), 0)


class AccountService_SendActivationEmail(AccountServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = user_pb2.Email(email=make_email("new"))

    def test(self):
        request = self._make_create_request()
        self.service.Create(request, self.grpc_context)
        user = get_user_model().objects.get(email=request.email)
        email = AccountActivationEmail(user.id)
        self.assertEmails([email])
        self.service.SendActivationEmail(self.request, self.grpc_context)
        self.assertEmails([email, email])

    def test_active_user(self):
        request = self._make_create_request()
        self.service.Create(request, self.grpc_context)
        user = get_user_model().objects.get(email=request.email)
        email = AccountActivationEmail(user.id)
        self.assertEmails([email])
        user.is_active = True
        user.save()
        self.service.SendActivationEmail(self.request, self.grpc_context)
        self.assertEmails([email])

    def test_non_existent_user(self):
        self.service.SendActivationEmail(self.request, self.grpc_context)
        self.assertEmails([])


class AccountService_ConfirmActivation(AccountServiceTestCase):
    def setUp(self):
        super().setUp()
        self.service.Create(self._make_create_request(), self.grpc_context)
        self.user: AbstractUser = get_user_model().objects.latest("date_joined")
        self.connection_count = Connection.objects.count()
        self.connection_client = user_pb2.Client(
            hardware=Hardware.UNKNOWN,
            software=Software.UNKNOWN,
        )
        self.request = user_pb2.ConnectionToken(
            token=AccountActivationEmail(self.user.id).token,
            client=self.connection_client,
        )

    def test(self):
        token = self.service.ConfirmActivation(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), self.connection_count + 1)
        connection = Connection.objects.latest("date_created")
        claims = jwt.decode(token.token)
        self.assertIn("user_id", claims)
        self.assertIn("connection_id", claims)
        self.assertEqual(claims["user_id"], str(self.user.id))
        self.assertEqual(claims["connection_id"], str(connection.id))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(connection.user, self.user)
        self.assertEqual(connection.hardware, Hardware.UNKNOWN)
        self.assertEqual(connection.software, Software.UNKNOWN)

    def test_missing_user_id(self):
        token = jwt.encode({"timestamp": now().timestamp()})
        self.request = user_pb2.ConnectionToken(
            token=token, client=self.connection_client
        )

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_expired_timestamp(self):
        token = jwt.encode(
            {
                "user_id": str(self.user.id),
                "timestamp": (now() + timedelta(days=-1)).timestamp(),
            }
        )
        self.request = user_pb2.ConnectionToken(
            token=token, client=self.connection_client
        )

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_already_active_user(self):
        self.user.is_active = True
        self.user.save()

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_deleted_user(self):
        self.user.delete()

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_banned_user(self):
        self.user.ban()

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_non_existent_user(self):
        token = jwt.encode(
            {"user_id": str(uuid.uuid4()), "timestamp": now().timestamp()}
        )
        self.request = user_pb2.ConnectionToken(
            token=token, client=self.connection_client
        )

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_missing_connection_id_or_timestamp(self):
        token = jwt.encode({"user_id": str(self.user.id)})
        self.request = user_pb2.ConnectionToken(
            token=token, client=self.connection_client
        )

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_user_id_connection_id_mismatch(self):
        connection = Connection.objects.create(user=self.main_user)
        token = jwt.encode(
            {
                "user_id": str(self.user.id),
                "connection_id": str(connection.id),
            }
        )
        self.request = user_pb2.ConnectionToken(
            token=token, client=self.connection_client
        )

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count + 1)

    def test_bad_client(self):
        self.request.client.hardware = "bad"

        with self.assertRaises(ValidationError):
            self.service.ConfirmActivation(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)


class AccountService_ListConnections(AccountServiceTestCase, AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        Connection.objects.create(user=self.other_user)
        Connection.objects.create(user=self.main_user)

    def test(self):
        connections = self.service.ListConnections(self.request, self.grpc_context)
        main_user_connections = Connection.objects.filter(user=self.main_user)
        ids = [c.id for c in connections.connections]
        self.assertEqual(len(ids), len(main_user_connections))

        for connection in Connection.objects.filter(user=self.main_user):
            self.assertIn(connection.id.bytes, ids)

        for connection in Connection.objects.filter(user=self.other_user):
            self.assertNotIn(connection.id.bytes, ids)

    def test_empty(self):
        Connection.objects.filter(user=self.main_user).delete()
        connections = self.service.ListConnections(self.request, self.grpc_context)
        self.assertEqual(len(connections.connections), 0)

    def test_client(self):
        connection = Connection.objects.create(
            user=self.main_user,
            hardware=Hardware.MOBILE,
            software=Software.ANDROID,
        )
        connections = self.service.ListConnections(self.request, self.grpc_context)
        message = next(
            c for c in connections.connections if c.id == connection.id.bytes
        )
        self.assertEqual(message.client.hardware, Hardware.MOBILE)
        self.assertEqual(message.client.software, Software.ANDROID)


class AccountService_SendConnectionEmail(AccountServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = user_pb2.Email(email=self.main_user.email)

    def test(self):
        self.service.SendConnectionEmail(self.request, self.grpc_context)
        self.assertEmails([AccountConnectionEmail(self.main_user.id)])

    def test_unused_email(self):
        self.request.email = make_email("bad")

        with self.assertRaises(ObjectDoesNotExist):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([])

    def test_bad_email(self):
        self.request.email = "not an email"

        with self.assertRaises(InvalidArgument):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([])

    def test_pending_user(self):
        self.main_user.is_active = False
        self.main_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([])

    def test_deleted_user(self):
        self.main_user.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([])

    def test_banned_user(self):
        self.main_user.ban()

        with self.assertRaises(PermissionDenied):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([UserBannedEmail(self.main_user.id)])

    def test_password(self):
        self.main_user.set_password("Random password")
        self.main_user.save()

        with self.assertRaises(Cancelled):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([])

    def test_temporary_ban(self):
        self.main_user.ban(timedelta(days=3))
        mail.outbox.clear()

        with self.assertRaises(PermissionDenied):
            self.service.SendConnectionEmail(self.request, self.grpc_context)

        self.assertEmails([])


class AccountService_ConfirmConnection(AccountServiceTestCase):
    def setUp(self):
        super().setUp()
        self.service.SendConnectionEmail(
            user_pb2.Email(email=self.main_user.email), self.grpc_context
        )
        self.connection_count = Connection.objects.count()
        self.request = user_pb2.ConnectionToken(
            token=AccountConnectionEmail(self.main_user.id).token,
            client=user_pb2.Client(
                hardware=Hardware.UNKNOWN, software=Software.UNKNOWN
            ),
        )

    def test(self):
        token = self.service.ConfirmConnection(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), self.connection_count + 1)
        connection = Connection.objects.latest("date_created")
        claims = jwt.decode(token.token)
        self.assertIn("user_id", claims)
        self.assertIn("connection_id", claims)
        self.assertEqual(claims["user_id"], str(self.main_user.id))
        self.assertEqual(claims["connection_id"], str(connection.id))
        self.assertEqual(connection.user, self.main_user)
        self.assertEqual(connection.hardware, Hardware.UNKNOWN)
        self.assertEqual(connection.software, Software.UNKNOWN)

    def test_twice(self):
        self.service.ConfirmConnection(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), self.connection_count + 1)

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmConnection(self.request, self.grpc_context)

    def test_pending_user(self):
        self.main_user.is_active = False
        self.main_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmConnection(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_deleted_user(self):
        self.main_user.delete()

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmConnection(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_banned_user(self):
        self.main_user.ban()

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmConnection(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_activation_token(self):
        self.request.token = AccountActivationEmail(self.main_user.id).token

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmConnection(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_expired_token(self):
        token = jwt.encode(
            {
                "user_id": str(self.main_user.id),
                "timestamp": (now() + timedelta(days=-2)).timestamp(),
                "connection_token": str(self.main_user.connection_token),
            }
        )
        self.request.token = token

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmConnection(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_bad_client(self):
        self.request.client.hardware = "bad"

        with self.assertRaises(ValidationError):
            self.service.ConfirmConnection(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)


class AccountService_Connect(AccountServiceTestCase):
    def setUp(self):
        super().setUp()
        password = "Random password"
        self.main_user.set_password(password)
        self.main_user.save()
        self.connection_count = Connection.objects.count()
        self.request = user_pb2.ConnectionCredentials(
            email=self.main_user.email,
            password=password,
            client=user_pb2.Client(
                hardware=Hardware.UNKNOWN, software=Software.UNKNOWN
            ),
        )

    def test(self):
        token = self.service.Connect(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), self.connection_count + 1)
        connection = Connection.objects.latest("date_created")
        claims = jwt.decode(token.token)
        self.assertIn("user_id", claims)
        self.assertIn("connection_id", claims)
        self.assertEqual(claims["user_id"], str(self.main_user.id))
        self.assertEqual(claims["connection_id"], str(connection.id))
        self.assertEqual(connection.user, self.main_user)
        self.assertEqual(connection.hardware, Hardware.UNKNOWN)
        self.assertEqual(connection.software, Software.UNKNOWN)

    def test_wrong_email(self):
        self.request.email = make_email("bad")

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Connect(self.request, self.grpc_context)

    def test_wrong_password(self):
        self.request.password = "bad"

        with self.assertRaises(InvalidArgument):
            self.service.Connect(self.request, self.grpc_context)

    def test_no_password(self):
        self.main_user.set_unusable_password()
        self.main_user.save()

        with self.assertRaises(InvalidArgument):
            self.service.Connect(self.request, self.grpc_context)

    def test_pending_user(self):
        self.main_user.is_active = False
        self.main_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Connect(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_banned_user(self):
        self.main_user.ban()

        with self.assertRaises(PermissionDenied):
            self.service.Connect(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_temporarily_banned_user(self):
        self.main_user.ban(timedelta(days=3))

        with self.assertRaises(PermissionDenied):
            self.service.Connect(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)

    def test_bad_client(self):
        self.request.client.hardware = "bad"

        with self.assertRaises(ValidationError):
            self.service.Connect(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), self.connection_count)


class AccountService_Disconnect(AccountServiceTestCase, AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.request = id_pb2.Id()

    def test(self):
        connection_count = Connection.objects.count()
        self.service.Disconnect(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), connection_count - 1)
        self.assertNotIn(self.main_connection.id, Connection.objects.values("id"))

    def test_specific(self):
        connection = Connection.objects.create(user=self.main_user)
        connection_count = Connection.objects.count()
        self.request.id = connection.id.bytes
        self.service.Disconnect(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), connection_count - 1)

    def test_other(self):
        connection = Connection.objects.create(user=self.other_user)
        connection_count = Connection.objects.count()
        self.request.id = connection.id.bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Disconnect(self.request, self.grpc_context)

        self.assertEqual(Connection.objects.count(), connection_count)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Disconnect(self.request, self.grpc_context)


class AccountService_DisconnectAll(AccountServiceTestCase, AuthenticatedTestCase):
    def test(self):
        Connection.objects.create(user=self.main_user)
        Connection.objects.create(user=self.other_user)
        connection_count = Connection.objects.count()
        self.service.DisconnectAll(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.count(), connection_count - 2)
        self.assertEqual(Connection.objects.filter(user=self.main_user).count(), 0)

    def test_no_connections(self):
        connection_count = Connection.objects.count()
        self.service.DisconnectAll(self.request, self.grpc_context)
        self.assertEqual(Connection.objects.filter(user=self.main_user).count(), 0)
        self.assertEqual(Connection.objects.count(), connection_count - 1)


class UserService_Retrieve(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        asset = open(get_asset("image.png"), "rb")
        self.other_user.bio = "Bio"
        self.other_user.avatar = ImageFile(file=asset, name="image.png")
        self.other_user.save()
        self.request = id_pb2.Id(id=self.other_user.id.bytes)

    def test(self):
        self.other_user.blocked_users.add(self.main_user)
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.other_user.id.bytes)
        self.assertAlmostEqual(
            datetime.fromtimestamp(user.date_joined.seconds, tz=get_current_timezone()),
            self.other_user.date_joined,
            delta=timedelta(seconds=1),
        )
        self.assertEqual(user.profile.rank, user_pb2.RANK_CITIZEN)
        self.assertFalse(user.profile.is_deleted)
        self.assertFalse(user.profile.is_banned)
        self.assertFalse(user.profile.is_blocked)
        self.assertEqual(user.profile.username, str(self.other_user.username))
        self.assertEqual(user.profile.avatar.url, get_image_url(self.other_user.avatar))
        self.assertEqual(user.bio, self.other_user.bio)
        self.assertEqual(user.email, "")
        self.assertEqual(user.blocked_users, 0)

    def test_banned_forever(self):
        self.other_user.ban()
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.other_user.id.bytes)
        self.assertAlmostEqual(
            datetime.fromtimestamp(user.date_joined.seconds, tz=get_current_timezone()),
            self.other_user.date_joined,
            delta=timedelta(seconds=1),
        )
        self.assertEqual(user.profile.rank, user_pb2.RANK_UNSPECIFIED)
        self.assertFalse(user.profile.is_deleted)
        self.assertTrue(user.profile.is_banned)
        self.assertEqual(user.profile.username, "")
        self.assertEqual(user.profile.avatar.url, "")
        self.assertEqual(user.bio, "")
        self.assertEqual(user.email, "")

    def test_blocked(self):
        self.main_user.blocked_users.add(self.other_user)
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.other_user.id.bytes)
        self.assertTrue(user.profile.is_blocked)

    def test_deleted(self):
        self.other_user.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Retrieve(self.request, self.grpc_context)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Retrieve(self.request, self.grpc_context)

    def test_anonymous(self):
        self.grpc_context.set_user(None)
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.other_user.id.bytes)
        self.assertEqual(user.email, "")
        self.assertEqual(user.blocked_users, 0)
        self.assertFalse(user.profile.is_blocked)

    def test_self(self):
        self.request.id = self.main_user.id.bytes
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.main_user.id.bytes)
        self.assertEqual(user.email, self.main_user.email)
        self.assertEqual(user.blocked_users, self.main_user.blocked_users.count())

    def test_staff_rank(self):
        self.other_user.is_staff = True
        self.other_user.save()
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.rank, user_pb2.RANK_STAFF)

    def test_superuser_rank(self):
        self.other_user.is_superuser = True
        self.other_user.save()
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.rank, user_pb2.RANK_SUPERUSER)

    def test_temporary_ban(self):
        self.other_user.ban(timedelta(days=3))
        user = self.service.Retrieve(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.other_user.id.bytes)
        self.assertTrue(user.profile.is_banned)
        self.assertEqual(user.profile.username, str(self.other_user.username))
        self.assertEqual(user.bio, self.other_user.bio)

    def test_empty_id(self):
        self.request.id = b""

        with self.assertRaises(InvalidArgument):
            self.service.Retrieve(self.request, self.grpc_context)


class UserService_RetrieveMe(UserServiceTestCase):
    def test(self):
        self.main_user.blocked_users.add(self.other_user)
        user = self.service.RetrieveMe(self.request, self.grpc_context)
        self.assertEqual(user.profile.id, self.main_user.id.bytes)
        self.assertEqual(user.profile.username, str(self.main_user.username))
        self.assertEqual(user.email, str(self.main_user.email))
        self.assertEqual(user.blocked_users, self.main_user.blocked_users.count())

    def test_date_last_used(self):
        before = now() - timedelta(days=1)
        Connection.objects.filter(id=self.main_connection.id).update(
            date_last_used=before
        )
        self.service.RetrieveMe(self.request, self.grpc_context)
        self.main_connection.refresh_from_db()
        self.assertGreater(self.main_connection.date_last_used, before)


class UserService_UpdateBio(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = user_pb2.Bio(bio="Hello")

    def test(self):
        self.service.UpdateBio(self.request, self.grpc_context)
        self.main_user.refresh_from_db()
        self.assertEqual(self.main_user.bio, self.request.bio)

    def test_empty(self):
        self.main_user.bio = "Bio"
        self.main_user.save()
        self.request.bio = ""
        self.service.UpdateBio(self.request, self.grpc_context)
        self.main_user.refresh_from_db()
        self.assertEqual(self.main_user.bio, self.request.bio)

    def test_too_long(self):
        self.request.bio = "a" * (get_user_model().bio.field.max_length + 1)

        with self.assertRaises(ValidationError):
            self.service.UpdateBio(self.request, self.grpc_context)

    def test_boundary_length(self):
        self.request.bio = "a" * get_user_model().bio.field.max_length
        self.service.UpdateBio(self.request, self.grpc_context)
        self.main_user.refresh_from_db()
        self.assertEqual(self.main_user.bio, self.request.bio)


class UserService_UpdateAvatar(ImageTestCaseMixin, UserServiceTestCase):
    def test(self):
        for extension in ("jpeg", "png"):
            avatar = self.service.UpdateAvatar(
                self.make_request(extension), self.grpc_context
            )
            self.main_user.refresh_from_db()
            regex = r".*\." + extension
            self.assertRegex(str(self.main_user.avatar), regex)
            self.assertRegex(avatar.url, regex)

    def test_empty(self):
        self.service.UpdateAvatar(self.make_request("jpeg"), self.grpc_context)
        self.service.UpdateAvatar([], self.grpc_context)
        self.main_user.refresh_from_db()
        self.assertEqual(str(self.main_user.avatar), "")

    def test_bad_format(self):
        with self.assertRaises(InvalidArgument):
            self.service.UpdateAvatar(self.make_request("txt"), self.grpc_context)


class UserService_SendEmailUpdateEmail(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = user_pb2.Email(email=make_email("new"))

    def test(self):
        self.service.SendEmailUpdateEmail(self.request, self.grpc_context)
        self.assertEmails(
            [UserEmailUpdateEmail(self.main_user.id, self.main_user.email)]
        )

    def test_invalid_email(self):
        self.request.email = "bad"

        with self.assertRaises(InvalidArgument):
            self.service.SendEmailUpdateEmail(self.request, self.grpc_context)

    def test_email_already_taken(self):
        self.request.email = self.other_user.email

        with self.assertRaises(AlreadyExists):
            self.service.SendEmailUpdateEmail(self.request, self.grpc_context)

    def test_own_email(self):
        self.request.email = self.main_user.email

        with self.assertRaises(AlreadyExists):
            self.service.SendEmailUpdateEmail(self.request, self.grpc_context)

        self.assertEmails([])


class UserService_ConfirmEmailUpdate(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = user_pb2.Token(token=self._make_token(self.main_user))

    def test(self):
        self.service.ConfirmEmailUpdate(self.request, self.grpc_context)
        self.main_user.refresh_from_db()
        self.assertEqual(self.main_user.email, make_email("new"))

    def test_other(self):
        self.request.token = self._make_token(self.other_user)

        with self.assertRaises(PermissionDenied):
            self.service.ConfirmEmailUpdate(self.request, self.grpc_context)

    def test_expired_token(self):
        token = jwt.encode(
            {
                "user_id": str(self.main_user.id),
                "email": make_email("new"),
                "timestamp": (now() + timedelta(days=-2)).timestamp(),
            }
        )
        self.request.token = token

        with self.assertRaises(Unauthenticated):
            self.service.ConfirmEmailUpdate(self.request, self.grpc_context)

        self.main_user.refresh_from_db()
        self.assertEqual(self.main_user.email, make_email("main"))

    def _make_token(self, user: User) -> str:
        return UserEmailUpdateEmail(user.id, make_email("new")).token


class UserService_ListBlocked(UserServiceTestCase, PaginationTestCase):
    main_pagination_field = "username"

    def setUp(self):
        super().setUp()
        self.users = self._create_test_users()
        self.main_user.blocked_users.set(self.users)
        self.out_of_bounds_cursor = pagination_pb2.Cursor(
            data=[
                pagination_pb2.KeyValuePair(
                    key=self.main_pagination_field, value="zzz"
                ),
                pagination_pb2.KeyValuePair(key="id", value=str(self.users[-1].id)),
            ],
            is_next=True,
        )

    def paginate(self, request_iterator: Iterator[pagination_pb2.Page]) -> Iterator:
        return self.service.ListBlocked(request_iterator, self.grpc_context)

    def check(self, item: user_pb2.Profile, position: int):
        self.assertEqual(item.id, self.users[position].id.bytes)
        self.assertEqual(item.username, self.users[position].username)

    def test(self):
        self.run_test(self.check)

    def test_previous(self):
        self.run_test_previous(self.check)

    def test_reverse(self):
        self.run_test_reverse(self.check)

    def test_reverse_previous(self):
        self.run_test_reverse_previous(self.check)

    def test_empty(self):
        self.run_test_empty(self.main_user.blocked_users.all())

    def test_invalid_size(self):
        self.run_test_invalid_size()

    def test_no_header(self):
        self.run_test_no_header()

    def test_out_of_bounds(self):
        self.run_test_out_of_bounds(self.out_of_bounds_cursor)

    def _create_test_users(self) -> list[AbstractUser]:
        return sorted(self._create_users(count=24), key=lambda u: u.username)


class UserService_UpdateBlock(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = user_pb2.Block(id=self.other_user.id.bytes, blocked=True)

    def test(self):
        self.service.UpdateBlock(self.request, self.grpc_context)
        self.assertEqual(
            [u.id for u in self.main_user.blocked_users.all()],
            [self.other_user.id],
        )

    def test_block_me(self):
        self.request.id = self.main_user.id.bytes

        with self.assertRaises(PermissionDenied):
            self.service.UpdateBlock(self.request, self.grpc_context)

    def test_block_already_blocked(self):
        self.main_user.blocked_users.add(self.other_user)
        self.service.UpdateBlock(self.request, self.grpc_context)
        self.assertEqual(
            [u.id for u in self.main_user.blocked_users.all()],
            [self.other_user.id],
        )

    def test_unblock(self):
        self.request.blocked = False
        self.service.UpdateBlock(self.request, self.grpc_context)
        self.assertEqual(self.main_user.blocked_users.count(), 0)

    def test_unblock_not_blocked(self):
        self.request.blocked = False
        self.main_user.blocked_users.remove(self.other_user)
        self.service.UpdateBlock(self.request, self.grpc_context)
        self.assertEqual(self.main_user.blocked_users.count(), 0)

    def test_block_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateBlock(self.request, self.grpc_context)


class UserService_Report(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = id_pb2.Id(id=self.other_user.id.bytes)

    def test_citizen_reports_citizen(self):
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(
            flag.target_type, ContentType.objects.get_for_model(get_user_model())
        )
        self.assertEqual(flag.target_id, str(self.other_user.id))

    def test_citizen_reports_staff(self):
        self.other_user.is_staff = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_citizen_reports_superuser(self):
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_staff_reports_citizen(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(
            flag.target_type, ContentType.objects.get_for_model(get_user_model())
        )
        self.assertEqual(flag.target_id, str(self.other_user.id))

    def test_staff_reports_staff(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.is_staff = True
        self.other_user.save()
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(
            flag.target_type, ContentType.objects.get_for_model(get_user_model())
        )
        self.assertEqual(flag.target_id, str(self.other_user.id))

    def test_staff_reports_superuser(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_superuser_reports_citizen(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(
            flag.target_type, ContentType.objects.get_for_model(get_user_model())
        )
        self.assertEqual(flag.target_id, str(self.other_user.id))

    def test_superuser_reports_staff(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.other_user.is_staff = True
        self.other_user.save()
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(
            flag.target_type, ContentType.objects.get_for_model(get_user_model())
        )
        self.assertEqual(flag.target_id, str(self.other_user.id))

    def test_superuser_reports_superuser(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.other_user.is_superuser = True
        self.other_user.save()
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(
            flag.target_type, ContentType.objects.get_for_model(get_user_model())
        )
        self.assertEqual(flag.target_id, str(self.other_user.id))

    def test_deleted(self):
        self.other_user.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Report(self.request, self.grpc_context)

    def test_self(self):
        self.request.id = self.main_user.id.bytes

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_not_active(self):
        self.other_user.is_active = False
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_banned(self):
        self.other_user.ban()

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_twice(self):
        self.service.Report(self.request, self.grpc_context)
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        notification = Notification.flag_objects.first()
        self.assertEqual(notification.count, 1)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Report(self.request, self.grpc_context)


class UserService_Absolve(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        Flag.objects.create(issuer=self.main_user, target=self.other_user)
        self.request = id_pb2.Id(id=self.other_user.id.bytes)

    def test_citizen_absolves_citizen(self):
        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_citizen_absolves_staff(self):
        self.other_user.is_staff = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_staff_absolves_superuser(self):
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_staff_absolves_citizen(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(get_user_model(), self.other_user.id)

    def test_staff_absolves_staff(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.is_staff = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_staff_absolves_superuser(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_superuser_absolves_citizen(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(get_user_model(), self.other_user.id)

    def test_superuser_absolves_staff(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.other_user.is_staff = True
        self.other_user.save()
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(get_user_model(), self.other_user.id)

    def test_superuser_absolves_superuser(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_empty(self):
        self.main_user.is_staff = True
        self.main_user.save()
        Notification.flag_objects.all().delete()
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(get_user_model(), self.other_user.id)

    def test_self(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.request.id = self.main_user.id.bytes

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_non_existent(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Absolve(self.request, self.grpc_context)


class UserService_Ban(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.before = now()
        self.request = user_pb2.BanSentence(id=self.other_user.id.bytes, days=3)

    def test_citizen_bans_citizen(self):
        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_citizen_bans_staff(self):
        self.other_user.is_staff = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_citizen_bans_superuser(self):
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_staff_bans_citizen(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.service.Ban(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_banned)
        self.assertAlmostEqual(
            self.other_user.date_ban_end,
            self.before + timedelta(days=self.request.days),
            delta=timedelta(seconds=1),
        )

    def test_staff_bans_staff(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.is_staff = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_staff_bans_superuser(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_superuser_bans_citizen(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.service.Ban(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_banned)
        self.assertAlmostEqual(
            self.other_user.date_ban_end,
            self.before + timedelta(days=self.request.days),
            delta=timedelta(seconds=1),
        )

    def test_superuser_bans_staff(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.other_user.is_staff = True
        self.other_user.save()
        self.service.Ban(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_banned)
        self.assertAlmostEqual(
            self.other_user.date_ban_end,
            self.before + timedelta(days=self.request.days),
            delta=timedelta(seconds=1),
        )

    def test_superuser_bans_superuser(self):
        self.main_user.is_superuser = True
        self.main_user.save()
        self.other_user.is_superuser = True
        self.other_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_no_duration(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.request.ClearField("days")
        self.service.Ban(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_banned)
        self.assertIsNone(self.other_user.date_ban_end)

    def test_other_already_banned(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.ban()
        self.service.Ban(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_banned)
        self.assertAlmostEqual(
            self.other_user.date_ban_end,
            self.before + timedelta(days=self.request.days),
            delta=timedelta(seconds=1),
        )

    def test_other_deleted(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.other_user.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Ban(self.request, self.grpc_context)

    def test_self(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.request.id = self.main_user.id.bytes

        with self.assertRaises(PermissionDenied):
            self.service.Ban(self.request, self.grpc_context)

    def test_non_existent(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Ban(self.request, self.grpc_context)

    @pytest.mark.xfail(
        reason=(
            "Huge 'days' values overflow timedelta inside User.ban and crash "
            "the RPC with UNKNOWN instead of being rejected as INVALID_ARGUMENT"
        ),
        strict=True,
    )
    def test_many_days(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.request.days = 2**32 - 1

        with self.assertRaises(InvalidArgument):
            self.service.Ban(self.request, self.grpc_context)


class UserService_Promote(UserServiceTestCase):
    def setUp(self):
        super().setUp()
        self.main_user.is_staff = True
        self.main_user.is_superuser = True
        self.main_user.save()
        self.request = user_pb2.Promotion(id=self.other_user.id.bytes)

    def test_citizen_to_staff(self):
        self.other_user.is_staff = False
        self.other_user.is_superuser = False
        self.other_user.save()
        self.request.rank = user_pb2.RANK_STAFF
        self.service.Promote(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_staff)
        self.assertFalse(self.other_user.is_superuser)

    def test_staff_to_superuser(self):
        self.other_user.is_staff = True
        self.other_user.is_superuser = False
        self.other_user.save()
        self.request.rank = user_pb2.RANK_SUPERUSER
        self.service.Promote(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_staff)
        self.assertTrue(self.other_user.is_superuser)

    def test_staff_to_citizen(self):
        self.other_user.is_staff = True
        self.other_user.is_superuser = False
        self.other_user.save()
        self.request.rank = user_pb2.RANK_CITIZEN
        self.service.Promote(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertFalse(self.other_user.is_staff)
        self.assertFalse(self.other_user.is_superuser)

    def test_superuser_to_staff(self):
        self.other_user.is_staff = True
        self.other_user.is_superuser = True
        self.other_user.save()
        self.request.rank = user_pb2.RANK_STAFF
        self.service.Promote(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertTrue(self.other_user.is_staff)
        self.assertFalse(self.other_user.is_superuser)

    def test_superuser_to_citizen(self):
        self.other_user.is_staff = True
        self.other_user.is_superuser = True
        self.other_user.save()
        self.request.rank = user_pb2.RANK_CITIZEN
        self.service.Promote(self.request, self.grpc_context)
        self.other_user.refresh_from_db()
        self.assertFalse(self.other_user.is_staff)
        self.assertFalse(self.other_user.is_superuser)

    def test_citizen_to_superuser(self):
        self.other_user.is_staff = False
        self.other_user.is_superuser = False
        self.other_user.save()
        self.request.rank = user_pb2.RANK_SUPERUSER

        with self.assertRaises(PermissionDenied):
            self.service.Promote(self.request, self.grpc_context)

    def test_to_unspecified(self):
        self.request.rank = user_pb2.RANK_UNSPECIFIED

        with self.assertRaises(InvalidArgument):
            self.service.Promote(self.request, self.grpc_context)

    def test_not_superuser(self):
        self.main_user.is_superuser = False
        self.main_user.save()
        self.request.rank = user_pb2.RANK_STAFF

        with self.assertRaises(PermissionDenied):
            self.service.Promote(self.request, self.grpc_context)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes
        self.request.rank = user_pb2.RANK_STAFF

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Promote(self.request, self.grpc_context)

    def test_deleted(self):
        self.other_user.delete()
        self.request.rank = user_pb2.RANK_STAFF

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Promote(self.request, self.grpc_context)

    @pytest.mark.xfail(
        reason=(
            "Promote lacks the user_is_caller guard that Report, Absolve and "
            "Ban all have, so superusers can change their own rank"
        ),
        strict=True,
    )
    def test_self(self):
        self.request.id = self.main_user.id.bytes
        self.request.rank = user_pb2.RANK_STAFF

        with self.assertRaises(PermissionDenied):
            self.service.Promote(self.request, self.grpc_context)


class Helpers(BaseUserTestCase):
    def test_normalize(self):
        for value, expected in [
            ("admin", "ADMIN"),
            ("Ádmin", "ADMIN"),
            ("ad.min", "ADMIN"),
            ("ad-min", "ADMIN"),
            ("bad username", "BADUSERNAME"),
            ("user_1", "USER_1"),
            ("", ""),
        ]:
            self.assertEqual(normalize(value), expected)

    def test_check_email(self):
        check_email(make_email("main"))

        with self.assertRaises(InvalidArgument):
            check_email("bad")

    def test_check_user(self):
        check_user(self.main_user)

    def test_check_user_pending(self):
        self.main_user.is_active = False
        self.main_user.save()

        with self.assertRaises(PermissionDenied):
            check_user(self.main_user)

    def test_check_user_deleted(self):
        self.main_user.delete()
        self.main_user.refresh_from_db()

        with self.assertRaises(PermissionDenied):
            check_user(self.main_user)

    def test_check_user_banned(self):
        self.main_user.ban(timedelta(days=3))

        with self.assertRaises(PermissionDenied):
            check_user(self.main_user)

    def test_check_user_banned_permanently(self):
        self.main_user.ban()

        with self.assertRaises(PermissionDenied):
            check_user(self.main_user)
