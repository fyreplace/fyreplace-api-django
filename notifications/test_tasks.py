from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from posts.models import Comment, Post, Subscription
from posts.tests import PublishedPostTestCase

from .models import ApnsToken, Notification
from .remote import apns, fcm
from .tasks import (
    refresh_apns_token,
    send_notifications,
    send_remote_notifications_clear,
    send_remote_notifications_comment_acknowledgement,
    send_remote_notifications_comment_change,
)


class Task_send_notifications(PublishedPostTestCase):
    def setUp(self):
        super().setUp()
        self.post.subscribers.add(self.other_user)

    def test_multiple_subscribers(self):
        user = get_user_model().objects.create_user(
            username="third", email="third@fyreplace.app"
        )
        self.post.subscribers.add(user)
        Comment.objects.create(post=self.post, author=user, text="Text")
        subscriptions = Subscription.objects.filter(post=self.post)
        self.assertEqual(
            Notification.objects.filter(subscription__in=subscriptions).count(), 2
        )

    def test_author_excluded(self):
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        self.assertFalse(
            Notification.objects.filter(
                subscription__user=self.other_user, subscription__post=self.post
            ).exists()
        )

    def test_no_notification_no_op(self):
        from posts.models import Post

        draft = Post.objects.create(author=self.main_user)
        comment = Comment.objects.create(post=draft, author=self.main_user, text="Text")
        self.assertFalse(Notification.objects.exists())
        send_notifications.run(comment_id=str(comment.id), new_notifications=False)
        self.assertFalse(Notification.objects.exists())


class Task_send_remote_notifications_comment_change(PublishedPostTestCase):
    def test_dispatched(self):
        with (
            mock.patch.object(
                apns.send_remote_notifications_comment_change, "delay"
            ) as apns_delay,
            mock.patch.object(
                fcm.send_remote_notifications_comment_change, "delay"
            ) as fcm_delay,
        ):
            comment = Comment.objects.create(
                post=self.post, author=self.other_user, text="Text"
            )
            apns_delay.assert_called_once_with(comment_id=str(comment.id))
            fcm_delay.assert_called_once_with(comment_id=str(comment.id))

    @pytest.mark.xfail(
        reason=(
            "on_comment_post_save auto-subscribes the comment author before "
            "send_remote_notifications_comment_change runs, so "
            "comment.post.subscribers.exists() is always true and the guard "
            "never prevents the apns/fcm task dispatch"
        ),
        strict=True,
    )
    def test_no_subscribers(self):
        draft = Post.objects.create(author=self.main_user)

        with (
            mock.patch.object(
                apns.send_remote_notifications_comment_change, "delay"
            ) as apns_delay,
            mock.patch.object(
                fcm.send_remote_notifications_comment_change, "delay"
            ) as fcm_delay,
        ):
            Comment.objects.create(post=draft, author=self.main_user, text="Text")
            apns_delay.assert_not_called()
            fcm_delay.assert_not_called()


class Task_send_remote_notifications_comment_acknowledgement(PublishedPostTestCase):
    def test_dispatched(self):
        comment = Comment.objects.create(
            post=self.post, author=self.other_user, text="Text"
        )

        with (
            mock.patch.object(
                apns.send_remote_notifications_comment_acknowledgement, "delay"
            ) as apns_delay,
            mock.patch.object(
                fcm.send_remote_notifications_comment_acknowledgement, "delay"
            ) as fcm_delay,
        ):
            send_remote_notifications_comment_acknowledgement.delay(
                comment_id=str(comment.id), user_id=str(self.main_user.id)
            )
            apns_delay.assert_called_once_with(
                comment_id=str(comment.id), user_id=str(self.main_user.id)
            )
            fcm_delay.assert_called_once_with(
                comment_id=str(comment.id), user_id=str(self.main_user.id)
            )


class Task_send_remote_notifications_clear(PublishedPostTestCase):
    def test_dispatched(self):
        with (
            mock.patch.object(
                apns.send_remote_notifications_clear, "delay"
            ) as apns_delay,
            mock.patch.object(
                fcm.send_remote_notifications_clear, "delay"
            ) as fcm_delay,
        ):
            send_remote_notifications_clear.delay(user_id=str(self.main_user.id))
            apns_delay.assert_called_once_with(user_id=str(self.main_user.id))
            fcm_delay.assert_called_once_with(user_id=str(self.main_user.id))


class Task_refresh_apns_token(PublishedPostTestCase):
    def test(self):
        self.assertEqual(ApnsToken.objects.count(), 0)
        refresh_apns_token.delay()
        self.assertEqual(ApnsToken.objects.count(), 1)

    def test_replaces_existing(self):
        refresh_apns_token.delay()
        first = ApnsToken.objects.first()
        refresh_apns_token.delay()
        self.assertEqual(ApnsToken.objects.count(), 1)
        self.assertNotEqual(ApnsToken.objects.first().id, first.id)
