from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.images import ImageFile
from django.db import IntegrityError
from django.utils.timezone import now
from google.protobuf import empty_pb2

from core.tests import get_asset
from protos import user_pb2

from .emails import UserBannedEmail
from .models import Block, Connection, Hardware, Software
from .tests import BaseUserTestCase, UserContext, make_email


class User_ban(BaseUserTestCase):
    def test(self):
        Connection.objects.create(user=self.main_user)
        before = now()
        duration = timedelta(days=3)
        self.main_user.ban(duration)
        self.assertTrue(self.main_user.is_banned)
        self.assertAlmostEqual(
            self.main_user.date_ban_end, (before + duration), delta=timedelta(seconds=1)
        )
        self.assertEqual(Connection.objects.filter(user=self.main_user).count(), 0)
        self.assertEmails([UserBannedEmail(self.main_user.id)])

    def test_forever(self):
        Connection.objects.create(user=self.main_user)
        self.main_user.ban()
        self.assertTrue(self.main_user.is_banned)
        self.assertIsNone(self.main_user.date_ban_end)
        self.assertEqual(Connection.objects.filter(user=self.main_user).count(), 0)
        self.assertEmails([UserBannedEmail(self.main_user.id)])


class User_str(BaseUserTestCase):
    def test(self):
        self.assertEqual(str(self.main_user), self.main_user.username)

    def test_banned(self):
        self.main_user.ban()
        self.assertEqual(str(self.main_user), f"Banned user {self.main_user.id}")

    def test_temporarily_banned(self):
        self.main_user.ban(timedelta(days=3))
        self.assertEqual(str(self.main_user), self.main_user.username)

    def test_deleted(self):
        self.main_user.delete()
        self.assertEqual(str(self.main_user), f"Deleted user {self.main_user.id}")


class User_properties(BaseUserTestCase):
    def test_alive(self):
        self.assertTrue(self.main_user.is_alive)
        self.assertTrue(self.main_user.is_alive_and_kicking)
        self.assertFalse(self.main_user.is_pending)

    def test_pending(self):
        self.main_user.is_active = False
        self.main_user.save()
        self.main_user.refresh_from_db()
        self.assertTrue(self.main_user.is_alive)
        self.assertFalse(self.main_user.is_alive_and_kicking)
        self.assertTrue(self.main_user.is_pending)

    def test_banned(self):
        self.main_user.ban()
        self.main_user.refresh_from_db()
        self.assertFalse(self.main_user.is_alive)
        self.assertFalse(self.main_user.is_alive_and_kicking)
        self.assertFalse(self.main_user.is_pending)

    def test_deleted(self):
        self.main_user.delete()
        self.main_user.refresh_from_db()
        self.assertFalse(self.main_user.is_alive)
        self.assertFalse(self.main_user.is_alive_and_kicking)
        self.assertFalse(self.main_user.is_pending)


class User_rank(BaseUserTestCase):
    def test_citizen(self):
        self.assertEqual(self.main_user.rank, user_pb2.RANK_CITIZEN)

    def test_staff(self):
        self.main_user.is_staff = True
        self.assertEqual(self.main_user.rank, user_pb2.RANK_STAFF)

    def test_superuser(self):
        self.main_user.is_staff = True
        self.main_user.is_superuser = True
        self.assertEqual(self.main_user.rank, user_pb2.RANK_SUPERUSER)


class User_queryset(BaseUserTestCase):
    def test_existing_objects(self):
        user = get_user_model().objects.create_user(
            username="extra", email=make_email("extra")
        )
        user_count = get_user_model().objects.count()
        user.delete()
        self.assertEqual(get_user_model().objects.count(), user_count)
        self.assertEqual(get_user_model().existing_objects.count(), user_count - 1)
        self.assertFalse(get_user_model().existing_objects.filter(id=user.id).exists())


class User_ordering(BaseUserTestCase):
    def test(self):
        get_user_model().objects.create_user(username="aaa", email=make_email("aaa"))
        usernames = list(get_user_model().objects.values_list("username", flat=True))
        self.assertEqual(usernames, sorted(usernames))


class User_perform_soft_delete(BaseUserTestCase):
    def test(self):
        avatar = ImageFile(open(get_asset("image.jpeg"), "rb"), name="image.jpeg")
        self.main_user.avatar = avatar
        self.main_user.save()
        self.main_user.delete()
        self.main_user.refresh_from_db()
        self.assertIsNone(self.main_user.username)
        self.assertIsNone(self.main_user.email)
        self.assertFalse(self.main_user.has_usable_password())
        self.assertEqual(self.main_user.bio, "")
        self.assertTrue(self.main_user.is_deleted)
        self.assertFalse(self.main_user.avatar)


class User_message_fields(BaseUserTestCase):
    def test_banned_forever_profile(self):
        self.main_user.ban()
        message = self.main_user.to_message(message_class=user_pb2.Profile)
        self.assertEqual(message.id, self.main_user.id.bytes)
        self.assertTrue(message.is_banned)
        self.assertEqual(message.rank, user_pb2.RANK_UNSPECIFIED)
        self.assertEqual(message.username, "")

    def test_banned_forever_other_class(self):
        self.main_user.ban()
        message = self.main_user.to_message(message_class=empty_pb2.Empty)
        self.assertEqual(message, empty_pb2.Empty())

    def test_no_context(self):
        message = self.main_user.to_message()
        self.assertEqual(message.email, "")
        self.assertEqual(message.blocked_users, 0)
        self.assertEqual(message.profile.username, self.main_user.username)

    def test_self_context(self):
        context = UserContext()
        context.set_user(self.main_user)
        message = self.main_user.to_message(context=context)
        self.assertEqual(message.email, self.main_user.email)

    def test_temporarily_banned_profile(self):
        self.main_user.ban(timedelta(days=3))
        message = self.main_user.to_message(message_class=user_pb2.Profile)
        self.assertEqual(message.id, self.main_user.id.bytes)
        self.assertTrue(message.is_banned)
        self.assertEqual(message.username, self.main_user.username)


class User_block(BaseUserTestCase):
    def test_unique(self):
        self.main_user.blocked_users.add(self.other_user)

        with self.assertRaises(IntegrityError):
            Block.objects.create(issuer=self.main_user, target=self.other_user)

    def test_issuer_not_target(self):
        with self.assertRaises(IntegrityError):
            self.main_user.blocked_users.add(self.main_user)


class Connection_str(BaseUserTestCase):
    def test(self):
        connection = Connection.objects.create(user=self.main_user)
        self.assertEqual(
            str(connection),
            f"{self.main_user}: {connection.hardware}/{connection.software} ({connection.date_created})",
        )


class Connection_constraints(BaseUserTestCase):
    def test_valid_client(self):
        connection = Connection(
            user=self.main_user, hardware=Hardware.MOBILE, software=Software.ANDROID
        )
        connection.full_clean()

    def test_bad_hardware(self):
        connection = Connection(user=self.main_user, hardware="bad")
        self.assertRaises(ValidationError, connection.full_clean)

    def test_bad_software(self):
        connection = Connection(user=self.main_user, software="bad")
        self.assertRaises(ValidationError, connection.full_clean)

    def test_bad_hardware_integrity(self):
        with self.assertRaises(IntegrityError):
            Connection.objects.create(user=self.main_user, hardware="bad")


class Connection_ordering(BaseUserTestCase):
    def test(self):
        old = Connection.objects.create(user=self.main_user)
        new = Connection.objects.create(user=self.main_user)
        Connection.objects.filter(id=old.id).update(
            date_last_used=now() - timedelta(days=1)
        )
        self.assertEqual(list(Connection.objects.all()), [old, new])


class Connection_message(BaseUserTestCase):
    def test(self):
        connection = Connection.objects.create(
            user=self.main_user, hardware=Hardware.MOBILE, software=Software.LINUX
        )
        message = connection.to_message()
        self.assertEqual(message.id, connection.id.bytes)
        self.assertEqual(message.client.hardware, Hardware.MOBILE)
        self.assertEqual(message.client.software, Software.LINUX)
