import pytest
from django.contrib.auth import get_user_model

from posts.models import Chapter, Comment, Post
from posts.tests import PublishedPostTestCase
from users.models import Connection

from .models import (
    ApnsToken,
    Flag,
    MessagingService,
    Notification,
    RemoteMessaging,
    remove_notifications_for,
)


class Comment_create(PublishedPostTestCase):
    def test(self):
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        notifications = Notification.objects.filter(subscription__user=self.other_user)
        self.assertEqual(notifications.count(), 0)
        notifications = Notification.objects.filter(subscription__user=self.main_user)
        self.assertEqual(notifications.count(), 1)
        notification = notifications.first()
        self.assertEqual(notification.subscription.user, self.main_user)
        self.assertEqual(notification.subscription.post, self.post)
        self.assertEqual(notification.count, 1)

    def test_same_author(self):
        Comment.objects.create(post=self.post, author=self.main_user, text="Text")
        notifications = Notification.objects.filter(subscription__user=self.main_user)
        self.assertEqual(notifications.count(), 0)


class Notification_save(PublishedPostTestCase):
    def test(self):
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        notifications = Notification.objects.filter(subscription__user=self.main_user)
        self.assertEqual(notifications.count(), 1)
        notification = notifications.first()
        self.assertEqual(notification.count, 1)
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        notification.refresh_from_db()
        self.assertEqual(notification.count, 2)

    def test_zero_deletes(self):
        comment = Comment.objects.create(
            post=self.post, author=self.other_user, text="Text"
        )
        subscription = self.post.subscriptions.get(user=self.main_user)
        notification = Notification.objects.get(subscription=subscription)
        self.assertEqual(notification.count, 1)

        subscription.last_comment_seen = comment
        subscription.save()
        notification.save()

        self.assertFalse(
            Notification.objects.filter(subscription=subscription).exists()
        )


class Notification_str(PublishedPostTestCase):
    def test(self):
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        notification = Notification.objects.first()
        self.assertEqual(
            str(notification),
            f"{notification.target_type}/{notification.target_id} ({notification.count})",
        )


class Flag_str(PublishedPostTestCase):
    def test(self):
        flag = Flag.objects.create(issuer=self.main_user, target=self.post)
        self.assertEqual(
            str(flag),
            f"{flag.target_type}/{flag.target_id}",
        )


class Flag_save(PublishedPostTestCase):
    def test(self):
        Flag.objects.create(issuer=self.main_user, target=self.post)
        notification = Notification.flag_objects.get(target_id=str(self.post.id))
        self.assertEqual(notification.count, 1)

        Flag.objects.create(issuer=self.other_user, target=self.post)
        notification.refresh_from_db()
        self.assertEqual(notification.count, 2)


class RemoveNotificationsFor(PublishedPostTestCase):
    def test(self):
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        Flag.objects.create(issuer=self.main_user, target=self.post)
        self.assertTrue(Notification.objects.exists())
        self.assertTrue(Flag.objects.exists())
        remove_notifications_for(self.post)
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(Flag.objects.exists())


class FilterReadableBy(PublishedPostTestCase):
    def setUp(self):
        super().setUp()
        self.third_user = get_user_model().objects.create_user(
            username="third", email="third@fyreplace.app"
        )
        self.post.subscribers.add(self.other_user)
        Comment.objects.create(post=self.post, author=self.third_user, text="Text")
        self.flagged_post = Post.objects.create(author=self.other_user)
        Chapter.objects.create(
            post=self.flagged_post,
            position=self.flagged_post.chapter_position(0),
            text="Text",
        )
        self.flagged_post.publish(anonymous=False)
        Flag.objects.create(issuer=self.main_user, target=self.flagged_post)

    def test_staff(self):
        self.main_user.is_staff = True
        self.main_user.save()
        notifications = Notification.objects.filter_readable_by(self.main_user)
        self.assertEqual(notifications.count(), 2)

    def test_non_staff(self):
        notifications = Notification.objects.filter_readable_by(self.other_user)
        self.assertEqual(notifications.count(), 1)
        self.assertFalse(notifications.filter(subscription__isnull=True).exists())

    def test_anonymous_subscription(self):
        self.main_user.is_staff = True
        self.main_user.save()
        notifications = Notification.objects.filter_readable_by(
            self.main_user, subscription__isnull=True
        )
        self.assertEqual(notifications.count(), 1)
        self.assertEqual(notifications.first().count, 1)

    def test_flag_on_multi_subscribed_target(self):
        Flag.objects.create(issuer=self.main_user, target=self.post)
        self.assertTrue(
            Notification.objects.filter(
                subscription__isnull=True, target_id=str(self.post.id)
            ).exists()
        )


class Notification_message_targets(PublishedPostTestCase):
    def test_comment_target(self):
        comment = Comment.objects.create(
            post=self.post, author=self.other_user, text="Text"
        )
        Flag.objects.create(issuer=self.main_user, target=comment)
        notification = Notification.flag_objects.get(target_id=str(comment.id))
        message = notification.to_message()
        self.assertEqual(message.count, 1)
        self.assertEqual(message.comment.id, comment.id.bytes)

    def test_user_target(self):
        Flag.objects.create(issuer=self.main_user, target=self.other_user)
        notification = Notification.flag_objects.get(target_id=str(self.other_user.id))
        message = notification.to_message()
        self.assertEqual(message.count, 1)
        self.assertEqual(message.user.id, self.other_user.id.bytes)

    @pytest.mark.xfail(
        reason=(
            "The is_flag message field is never populated: it is a queryset "
            "annotation, so hasattr(instance, 'is_flag') is always False and "
            "the field defaults to False in every notification message"
        ),
        strict=True,
    )
    def test_flag_message(self):
        Flag.objects.create(issuer=self.main_user, target=self.post)
        notification = Notification.flag_objects.get(target_id=str(self.post.id))
        message = notification.to_message()
        self.assertTrue(message.is_flag)


class RemoteMessaging_str(PublishedPostTestCase):
    def test(self):
        connection = Connection.objects.create(user=self.main_user)
        messaging = RemoteMessaging.objects.create(
            connection=connection, service=MessagingService.FCM, token="token"
        )
        self.assertEqual(str(messaging), "token")


class ApnsToken_str(PublishedPostTestCase):
    def test(self):
        token = ApnsToken.objects.create(token="token")
        self.assertEqual(str(token), "token")
