from typing import Iterator, List
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils.timezone import now

from core import jwt
from core.tests import PaginationTestCase
from notifications.pagination import NotificationPaginationAdapter
from posts.models import Comment, Subscription
from posts.test_services import CommentServiceTestCase, PostServiceTestCase
from protos import notification_pb2, pagination_pb2
from users.models import Connection

from .models import Flag, MessagingService, Notification, RemoteMessaging
from .services import NotificationService


class NotificationServiceTestCase(PostServiceTestCase, CommentServiceTestCase):
    def setUp(self):
        super().setUp()
        self.main_posts = self._create_posts(
            author=self.main_user, count=3, published=True, anonymous=False
        )
        self.other_posts = self._create_posts(
            author=self.other_user, count=3, published=True, anonymous=False
        )
        self.service = NotificationService()


class NotificationService_Count(NotificationServiceTestCase):
    def test(self):
        for i, post in enumerate(self.main_posts):
            for _ in range(i + 1):
                Comment.objects.create(author=self.other_user, post=post, text="Text")

        count = self.service.Count(self.request, self.grpc_context)
        self.assertEqual(count.count, Comment.objects.count())

    def test_no_subscription(self):
        Comment.objects.create(
            author=self.other_user, post=self.other_posts[0], text="Text"
        )
        count = self.service.Count(self.request, self.grpc_context)
        self.assertEqual(count.count, 0)

    def test_self_comment(self):
        Comment.objects.create(
            author=self.main_user, post=self.main_posts[0], text="Text"
        )
        count = self.service.Count(self.request, self.grpc_context)
        self.assertEqual(count.count, 0)

    def test_flags(self):
        self.main_user.is_staff = True
        self.main_user.save()

        for post in self.other_posts:
            Flag.objects.create(issuer=self.main_user, target=post)

        count = self.service.Count(self.request, self.grpc_context)
        self.assertEqual(count.count, len(self.other_posts))

    def test_flags_and_subscriptions_sum(self):
        self.main_user.is_staff = True
        self.main_user.save()
        Comment.objects.create(
            author=self.other_user, post=self.main_posts[0], text="Text"
        )

        for post in self.other_posts:
            Flag.objects.create(issuer=self.main_user, target=post)

        count = self.service.Count(self.request, self.grpc_context)
        self.assertEqual(count.count, len(self.other_posts) + 1)

    def test_flags_not_staff(self):
        for post in self.other_posts:
            Flag.objects.create(issuer=self.main_user, target=post)

        count = self.service.Count(self.request, self.grpc_context)
        self.assertEqual(count.count, 0)


class NotificationService_List(NotificationServiceTestCase, PaginationTestCase):
    main_pagination_field = "date_updated"

    def setUp(self):
        super().setUp()
        self.notifications = self._create_test_notifications()
        self.out_of_bounds_cursor = pagination_pb2.Cursor(
            data=[
                pagination_pb2.KeyValuePair(key="importance", value="2"),
                pagination_pb2.KeyValuePair(
                    key=self.main_pagination_field, value=str(now())
                ),
                pagination_pb2.KeyValuePair(
                    key="id", value=str(self.notifications[-1].id)
                ),
            ],
            is_next=True,
        )

    def paginate(self, request_iterator: Iterator[pagination_pb2.Page]) -> Iterator:
        return self.service.List(request_iterator, self.grpc_context)

    def check(self, item: notification_pb2.Notification, position: int):
        self.assertEqual(
            item.post.id, UUID(self.notifications[position].target_id).bytes
        )
        self.assertEqual(item.count, self.notifications[position].count)

    def test(self):
        self.run_test(self.check)

    def test_previous(self):
        self.run_test_previous(self.check)

    def test_reverse(self):
        self.run_test_reverse(self.check)

    def test_reverse_previous(self):
        self.run_test_reverse_previous(self.check)

    def test_empty(self):
        self.run_test_empty(self.post.comments.all())

    def test_invalid_size(self):
        self.run_test_invalid_size()

    def test_no_header(self):
        self.run_test_no_header()

    def test_out_of_bounds(self):
        self.run_test_out_of_bounds(self.out_of_bounds_cursor)

    def _create_test_notifications(self) -> List[Notification]:
        self._create_comments(author=self.other_user, count=14)
        self._create_comments(author=self.main_user, count=4)

        posts = self._create_posts(author=self.main_user, count=14, published=True)
        posts += self._create_posts(author=self.other_user, count=4, published=True)
        posts += self._create_posts(author=self.main_user, count=10, published=True)

        for i, post in enumerate(posts):
            for _ in range(i + 1):
                Comment.objects.create(post=post, author=self.other_user, text="Text")

        notifications = Notification.objects.filter(subscription__user=self.main_user)
        adapter = NotificationPaginationAdapter(self.grpc_context, notifications)
        return list(notifications.order_by(*adapter.get_cursor_fields()))


class NotificationService_Clear(NotificationServiceTestCase):
    def test(self):
        for i in range(len(self.main_posts)):
            for _ in range(i + 1):
                Comment.objects.create(
                    author=self.other_user, post=self.main_posts[i], text="Text"
                )

        self.service.Clear(self.request, self.grpc_context)
        self.assertEqual(
            Notification.objects.filter(subscription__user=self.main_user).count(),
            0,
        )

    def test_last_comment_seen_advanced(self):
        comments = []

        for _ in range(3):
            comments.append(
                Comment.objects.create(
                    author=self.other_user, post=self.main_posts[0], text="Text"
                )
            )

        subscription = Subscription.objects.get(
            user=self.main_user, post=self.main_posts[0]
        )
        self.assertIsNone(subscription.last_comment_seen)
        self.service.Clear(self.request, self.grpc_context)
        subscription.refresh_from_db()
        self.assertEqual(subscription.last_comment_seen, comments[-1])

    def test_flags_survive(self):
        self.main_user.is_staff = True
        self.main_user.save()
        Flag.objects.create(issuer=self.main_user, target=self.other_posts[0])
        self.assertEqual(Notification.flag_objects.count(), 1)

        self.service.Clear(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)


class NotificationService_ListFlags(NotificationServiceTestCase):
    def test(self):
        self.main_user.is_staff = True
        self.main_user.save()
        Comment.objects.create(
            author=self.other_user, post=self.main_posts[0], text="Text"
        )

        for post in self.other_posts[:2]:
            Flag.objects.create(issuer=self.main_user, target=post)

        requests = iter(
            [
                pagination_pb2.Page(header=pagination_pb2.Header(forward=True, size=3)),
                pagination_pb2.Page(cursor=pagination_pb2.Cursor(is_next=True)),
            ]
        )
        items = next(self.service.List(requests, self.grpc_context))
        notifications = list(items.notifications)
        self.assertEqual(len(notifications), 3)
        self.assertFalse(notifications[0].is_flag)
        self.assertTrue(notifications[1].is_flag)
        self.assertTrue(notifications[2].is_flag)
        self.assertEqual(notifications[0].post.id, self.main_posts[0].id.bytes)
        self.assertIn(
            notifications[1].post.id,
            [post.id.bytes for post in self.other_posts[:2]],
        )
        self.assertIn(
            notifications[2].post.id,
            [post.id.bytes for post in self.other_posts[:2]],
        )
        self.assertCountEqual(
            [n.post.id for n in notifications[1:]],
            [post.id.bytes for post in self.other_posts[:2]],
        )


class NotificationService_RegisterToken(NotificationServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = notification_pb2.MessagingToken(
            service=notification_pb2.MESSAGING_SERVICE_UNSPECIFIED, token="token"
        )

    def test_apns(self):
        self.request.service = notification_pb2.MESSAGING_SERVICE_APNS
        self.service.RegisterToken(self.request, self.grpc_context)
        self.assertEqual(RemoteMessaging.objects.count(), 1)
        messaging = RemoteMessaging.objects.first()
        self.assertEqual(messaging.service, MessagingService.APNS)
        self.assertEqual(messaging.token, self.request.token)

    def test_fcm(self):
        self.request.service = notification_pb2.MESSAGING_SERVICE_FCM
        self.service.RegisterToken(self.request, self.grpc_context)
        self.assertEqual(RemoteMessaging.objects.count(), 1)
        messaging = RemoteMessaging.objects.first()
        self.assertEqual(messaging.service, MessagingService.FCM)
        self.assertEqual(messaging.token, self.request.token)

    def test_invalid_service(self):
        self.request.service = notification_pb2.MESSAGING_SERVICE_UNSPECIFIED

        with self.assertRaises(ValidationError):
            self.service.RegisterToken(self.request, self.grpc_context)

        self.assertFalse(RemoteMessaging.objects.exists())

    def test_replaces_other_tokens(self):
        self.request.service = notification_pb2.MESSAGING_SERVICE_APNS
        self.service.RegisterToken(self.request, self.grpc_context)
        self.request.token = "other-token"
        self.service.RegisterToken(self.request, self.grpc_context)
        self.assertEqual(RemoteMessaging.objects.count(), 1)
        self.assertEqual(RemoteMessaging.objects.first().token, "other-token")

    def test_updates_same_token(self):
        self.request.service = notification_pb2.MESSAGING_SERVICE_APNS
        self.service.RegisterToken(self.request, self.grpc_context)
        self.service.RegisterToken(self.request, self.grpc_context)
        self.assertEqual(RemoteMessaging.objects.count(), 1)
        self.assertEqual(RemoteMessaging.objects.first().token, self.request.token)

    def test_too_long_token(self):
        self.request.service = notification_pb2.MESSAGING_SERVICE_APNS
        self.request.token = "a" * 301

        with self.assertRaises(ValidationError):
            self.service.RegisterToken(self.request, self.grpc_context)

        self.assertFalse(RemoteMessaging.objects.exists())

    def test_duplicate_token(self):
        from users.tests import UserContext

        self.request.service = notification_pb2.MESSAGING_SERVICE_APNS
        self.service.RegisterToken(self.request, self.grpc_context)
        other_connection = Connection.objects.create(user=self.main_user)
        context = UserContext()
        context.set_user(self.main_user)
        token = jwt.encode(
            {
                "user_id": str(self.main_user.id),
                "connection_id": str(other_connection.id),
            }
        )
        context._invocation_metadata["authorization"] = f"Bearer {token}"
        context.caller_connection = other_connection

        with self.assertRaises(IntegrityError):
            self.service.RegisterToken(self.request, context)

        self.assertEqual(RemoteMessaging.objects.count(), 1)
