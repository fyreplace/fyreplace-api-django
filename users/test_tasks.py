from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ObjectDoesNotExist
from django.test import override_settings
from django.utils.timezone import now

from core.tests import get_asset

from .emails import UserEmailUpdateEmail
from .models import Connection
from .tasks import (
    cleanup_connections,
    cleanup_users,
    fetch_default_user_avatar,
    lift_ban,
    remove_user_data,
    send_user_email_update_email,
    use_connection,
)
from .tests import BaseUserTestCase, make_email


class Task_cleanup_users(BaseUserTestCase):
    def test(self):
        get_user_model().objects.create_user(
            username="new",
            is_active=False,
            date_joined=now() - settings.FYREPLACE_INACTIVE_USER_DURATION,
        )
        user_count = get_user_model().existing_objects.count()
        cleanup_users.delay()
        self.assertEqual(get_user_model().existing_objects.count(), user_count - 1)

    def test_too_soon(self):
        get_user_model().objects.create_user(
            username="new",
            is_active=False,
            date_joined=now()
            - (settings.FYREPLACE_INACTIVE_USER_DURATION - timedelta(hours=1)),
        )
        user_count = get_user_model().existing_objects.count()
        cleanup_users.delay()
        self.assertEqual(get_user_model().existing_objects.count(), user_count)

    def test_active_user(self):
        get_user_model().objects.create_user(
            username="new",
            is_active=True,
            date_joined=now() - settings.FYREPLACE_INACTIVE_USER_DURATION,
        )
        user_count = get_user_model().existing_objects.count()
        cleanup_users.delay()
        self.assertEqual(get_user_model().existing_objects.count(), user_count)

    def test_deleted_user(self):
        user = get_user_model().objects.create_user(
            username="new",
            is_active=False,
            date_joined=now() - settings.FYREPLACE_INACTIVE_USER_DURATION,
        )
        user.delete()
        user_count = get_user_model().objects.count()
        cleanup_users.delay()
        self.assertEqual(get_user_model().objects.count(), user_count)


class Task_cleanup_connections(BaseUserTestCase):
    def test(self):
        connection = Connection.objects.create(user=self.main_user)
        connection_count = Connection.objects.count()
        last_used = now() - settings.FYREPLACE_CONNECTION_DURATION
        Connection.objects.filter(id=connection.id).update(date_last_used=last_used)
        cleanup_connections.delay()
        self.assertEqual(Connection.objects.count(), connection_count - 1)

    def test_too_soon(self):
        connection = Connection.objects.create(user=self.main_user)
        connection_count = Connection.objects.count()
        last_used = now() - (settings.FYREPLACE_CONNECTION_DURATION - timedelta(days=1))
        Connection.objects.filter(id=connection.id).update(date_last_used=last_used)
        cleanup_connections.delay()
        self.assertEqual(Connection.objects.count(), connection_count)


class Task_remove_user_data(BaseUserTestCase):
    def test(self):
        Connection.objects.create(user=self.main_user)
        Connection.objects.create(user=self.main_user)
        Connection.objects.create(user=self.other_user)
        remove_user_data.delay(user_id=str(self.main_user.id))
        self.assertEqual(Connection.objects.filter(user=self.main_user).count(), 0)
        self.assertEqual(Connection.objects.filter(user=self.other_user).count(), 1)

    def test_non_existent_user(self):
        remove_user_data.delay(user_id=str(uuid4()))


class Task_use_connection(BaseUserTestCase):
    def test(self):
        connection = Connection.objects.create(user=self.main_user)
        before = now() - timedelta(days=1)
        Connection.objects.filter(id=connection.id).update(date_last_used=before)
        use_connection.delay(connection_id=str(connection.id))
        connection.refresh_from_db()
        self.assertGreater(connection.date_last_used, before)

    def test_non_existent_connection(self):
        use_connection.delay(connection_id=str(uuid4()))


class Task_lift_ban(BaseUserTestCase):
    def test(self):
        self.main_user.ban(timedelta(days=3))
        self.assertTrue(self.main_user.is_banned)
        lift_ban.delay(user_id=str(self.main_user.id))
        self.main_user.refresh_from_db()
        self.assertFalse(self.main_user.is_banned)
        self.assertIsNone(self.main_user.date_ban_end)

    def test_non_existent_user(self):
        lift_ban.delay(user_id=str(uuid4()))


class Task_fetch_default_user_avatar(BaseUserTestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="gravatar", email=make_email("gravatar")
        )

    def tearDown(self):
        self.user.avatar.delete(save=False)
        self.user.delete()
        super().tearDown()

    @override_settings(GRAVATAR_BASE_URL="https://gravatar.example.org")
    def test_non_existent_user(self):
        with self.assertRaises(ObjectDoesNotExist):
            fetch_default_user_avatar.run(user_id=str(uuid4()))

    @override_settings(GRAVATAR_BASE_URL="https://gravatar.example.org")
    def test_non_existent_user_propagates(self):
        with self.assertRaises(ObjectDoesNotExist):
            fetch_default_user_avatar.delay(user_id=str(uuid4()))

    @override_settings(GRAVATAR_BASE_URL=None)
    def test_disabled(self):
        fetch_default_user_avatar.delay(user_id=str(self.user.id))
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    @override_settings(GRAVATAR_BASE_URL="https://gravatar.example.org")
    def test_has_avatar(self):
        from django.core.files.images import ImageFile

        self.user.avatar = ImageFile(
            open(get_asset("image.jpeg"), "rb"), name="image.jpeg"
        )
        self.user.save()
        avatar_name = self.user.avatar.name

        with mock.patch("users.tasks.get") as get:
            fetch_default_user_avatar.delay(user_id=str(self.user.id))
            get.assert_not_called()

        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar.name, avatar_name)

    @override_settings(GRAVATAR_BASE_URL="https://gravatar.example.org")
    def test_success(self):
        content = open(get_asset("image.jpeg"), "rb").read()
        response = mock.MagicMock(is_success=True, content=content)

        with mock.patch("users.tasks.get", return_value=response):
            fetch_default_user_avatar.delay(user_id=str(self.user.id))

        self.user.refresh_from_db()
        self.assertTrue(self.user.avatar)
        self.assertTrue(self.user.avatar.name.endswith(".jpeg"))

    @override_settings(GRAVATAR_BASE_URL="https://gravatar.example.org")
    def test_not_found(self):
        response = mock.MagicMock(is_success=False)

        with mock.patch("users.tasks.get", return_value=response):
            fetch_default_user_avatar.delay(user_id=str(self.user.id))

        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    @override_settings(GRAVATAR_BASE_URL="https://gravatar.example.org")
    def test_bad_mime(self):
        content = open(get_asset("image.txt"), "rb").read()
        response = mock.MagicMock(is_success=True, content=content)

        with mock.patch("users.tasks.get", return_value=response):
            fetch_default_user_avatar.delay(user_id=str(self.user.id))

        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)


class Task_send_emails(BaseUserTestCase):
    def test_email_update(self):
        send_user_email_update_email.delay(
            user_id=str(self.main_user.id), email=make_email("new")
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [make_email("new")])
        self.assertEqual(
            mail.outbox[0].subject,
            UserEmailUpdateEmail(self.main_user.id, make_email("new")).subject,
        )

    def test_banned(self):
        from .tasks import send_user_banned_email

        send_user_banned_email.delay(user_id=str(self.main_user.id))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [make_email("main")])
