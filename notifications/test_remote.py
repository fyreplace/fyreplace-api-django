from types import SimpleNamespace
from unittest import TestCase, mock
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import messaging as firebase_messaging

from posts.models import Comment
from posts.tests import BaseCommentTestCase, PublishedPostTestCase
from users.models import Connection

from .models import (
    ApnsToken,
    MessagingService,
    RemoteMessaging,
    count_notifications_for,
)
from .remote import apns, b64encode, cut_text, fcm


class B64encode(TestCase):
    def test_bytes(self):
        self.assertEqual(b64encode(b"\xff\xff"), "__8")

    def test_uuid(self):
        original = uuid4()
        self.assertEqual(b64encode(original), b64encode(original.bytes))

    def test_padding(self):
        self.assertEqual(b64encode(b"\xff", padding=True), "_w==")


class CutText(TestCase):
    def test_short(self):
        self.assertEqual(cut_text("hello"), "hello")

    def test_empty(self):
        self.assertEqual(cut_text(""), "")

    def test_exactly_250(self):
        self.assertEqual(cut_text("a" * 250), "a" * 250)

    def test_251(self):
        self.assertEqual(cut_text("a" * 251), "a" * 249 + "\u2026")

    def test_long(self):
        self.assertEqual(cut_text("a" * 1000), "a" * 249 + "\u2026")


class FcmMakePayload(BaseCommentTestCase):
    def test(self):
        payload = fcm.make_payload(self.comment, "comment:creation")
        self.assertEqual(payload["_command"], "comment:creation")
        self.assertIn("comment", payload)
        self.assertEqual(payload["postId"], b64encode(self.comment.post_id))

    def test_clear(self):
        payload = fcm.make_payload(None, "notifications:clear")
        self.assertEqual(payload, {"_command": "notifications:clear"})


class FcmMakeNotificationTag(BaseCommentTestCase):
    def test(self):
        tag = fcm.make_notification_tag(self.comment)
        self.assertEqual(
            tag,
            f"{b64encode(self.comment.post_id)}:{b64encode(self.comment.id)}",
        )


class FcmMakeMulticastMessage(BaseCommentTestCase):
    def test_silent(self):
        payload = fcm.make_payload(self.comment, "comment:acknowledgement")
        message = fcm.make_multicast_message([], "channel", payload, self.comment)
        self.assertEqual(message.tokens, [])
        self.assertIsNone(message.android.notification)
        self.assertEqual(
            dict(message.android.data),
            {"_fcm.channel": "channel", **payload},
        )

    def test_notification(self):
        payload = fcm.make_payload(self.comment, "comment:creation")
        message = fcm.make_multicast_message([], "channel", payload, self.comment)
        notification = message.android.notification
        self.assertEqual(notification.title, self.comment.author.username)
        self.assertEqual(notification.body, cut_text(self.comment.text))
        self.assertEqual(notification.tag, fcm.make_notification_tag(self.comment))
        self.assertEqual(notification.channel_id, "channel")
        self.assertEqual(notification.event_timestamp, self.comment.date_created)
        self.assertEqual(dict(message.android.data), payload)


class FcmSendMulticastMessage(BaseCommentTestCase):
    def test_disabled(self):
        message = fcm.make_multicast_message([], "", {"_command": "x"}, None)
        self.assertIsNone(fcm.send_multicast_message(message))

    @override_settings(FIREBASE_APP="fake")
    def test_enabled(self):
        message = fcm.make_multicast_message([], "", {"_command": "x"}, None)

        with mock.patch.object(
            firebase_messaging, "send_each_for_multicast", return_value="sent"
        ) as send:
            self.assertEqual(fcm.send_multicast_message(message), "sent")
            send.assert_called_once_with(message)


class FakeError(Exception):
    code = "unknown-error"


def make_batch_response(codes):
    return SimpleNamespace(
        responses=[
            (
                SimpleNamespace(exception=None)
                if code is None
                else SimpleNamespace(exception=SimpleNamespace(code=code))
            )
            for code in codes
        ]
    )


class FcmRemoteChangeTestCase(PublishedPostTestCase):
    def setUp(self):
        super().setUp()
        self.third_user = get_user_model().objects.create_user(
            username="third", email="third@fyreplace.app"
        )
        self.own_messagings = self._create_messagings(self.main_user, ["dead-token"])
        self.other_messagings = self._create_messagings(
            self.other_user, ["other-token"]
        )
        self.comment = Comment.objects.create(
            post=self.post, author=self.third_user, text="Text"
        )

    def _create_messagings(self, user, tokens):
        messagings = []

        for token in tokens:
            connection = Connection.objects.create(user=user)
            messagings.append(
                RemoteMessaging.objects.create(
                    connection=connection,
                    service=MessagingService.FCM,
                    token=token,
                )
            )

        return messagings

    @staticmethod
    def _fake_send(message):
        return SimpleNamespace(
            responses=[
                (
                    SimpleNamespace(exception=None)
                    if token.startswith("live") or token.startswith("other")
                    else SimpleNamespace(
                        exception=SimpleNamespace(code=firebase_exceptions.NOT_FOUND)
                    )
                )
                for token in message.tokens
            ]
        )


class FcmDeadTokens(FcmRemoteChangeTestCase):
    @override_settings(FIREBASE_APP="fake")
    def test_dead_token_pruned(self):
        with mock.patch.object(
            firebase_messaging, "send_each_for_multicast", side_effect=self._fake_send
        ):
            fcm.send_remote_notifications_comment_change.run(
                comment_id=str(self.comment.id)
            )

        self.assertFalse(RemoteMessaging.objects.filter(token="dead-token").exists())
        self.assertTrue(RemoteMessaging.objects.filter(token="other-token").exists())

    @override_settings(FIREBASE_APP="fake")
    def test_live_token_kept(self):
        self._create_messagings(self.main_user, ["live-token"])

        with mock.patch.object(
            firebase_messaging, "send_each_for_multicast", side_effect=self._fake_send
        ):
            fcm.send_remote_notifications_comment_change.run(
                comment_id=str(self.comment.id)
            )

        self.assertTrue(RemoteMessaging.objects.filter(token="live-token").exists())
        self.assertTrue(RemoteMessaging.objects.filter(token="other-token").exists())

    @override_settings(FIREBASE_APP="fake")
    def test_dead_token_after_live_token(self):
        self._create_messagings(self.main_user, ["token-a"])

        def fake_send(message):
            return make_batch_response(
                [None, firebase_exceptions.NOT_FOUND]
                if len(message.tokens) == 2
                else [None]
            )

        with mock.patch.object(
            firebase_messaging, "send_each_for_multicast", side_effect=fake_send
        ):
            fcm.send_remote_notifications_comment_change.run(
                comment_id=str(self.comment.id)
            )

        self.assertEqual(RemoteMessaging.objects.count(), 2)

    @override_settings(FIREBASE_APP="fake")
    def test_error_raises(self):
        def fake_send(message):
            return SimpleNamespace(
                responses=[
                    SimpleNamespace(exception=FakeError("boom")) for _ in message.tokens
                ]
            )

        with (
            mock.patch.object(
                firebase_messaging,
                "send_each_for_multicast",
                side_effect=fake_send,
            ),
            self.assertRaises(FakeError),
        ):
            fcm.send_remote_notifications_comment_change.run(
                comment_id=str(self.comment.id)
            )


class ApnsHelpersTestCase(BaseCommentTestCase):
    @override_settings(APNS_URL="https://apns.example.org")
    def test_make_url(self):
        self.assertEqual(
            apns.make_url("token"),
            "https://apns.example.org/3/device/token",
        )

    def test_make_headers(self):
        ApnsToken.objects.create(token="jwt")
        headers = apns.make_headers("comment:creation")
        self.assertEqual(headers["authorization"], "bearer jwt")
        self.assertEqual(headers["apns-push-type"], "alert")
        self.assertEqual(headers["apns-topic"], settings.APPLE_APP_ID)
        headers = apns.make_headers("comment:acknowledgement")
        self.assertEqual(headers["apns-push-type"], "background")

    def test_make_payload(self):
        payload = apns.make_payload(self.comment, "comment:creation")
        self.assertEqual(payload["_command"], "comment:creation")
        self.assertIn("comment", payload)
        self.assertEqual(payload["postId"], b64encode(self.comment.post_id))
        payload = apns.make_payload(None, "notifications:clear")
        self.assertEqual(payload, {"_command": "notifications:clear"})

    def test_make_json_silent(self):
        payload = apns.make_payload(self.comment, "comment:acknowledgement")
        json = apns.make_json(payload, self.comment, self.main_user)
        self.assertEqual(json["aps"], {"content-available": 1})
        self.assertEqual(json["_aps.badge"], count_notifications_for(self.main_user))
        self.assertEqual(json["_aps.relevance-score"], 1.0)

    def test_make_json_notification(self):
        payload = apns.make_payload(self.comment, "comment:creation")
        json = apns.make_json(payload, self.comment, self.other_user)
        self.assertEqual(json["aps"]["alert"]["title"], self.comment.author.username)
        self.assertEqual(json["aps"]["alert"]["body"], cut_text(self.comment.text))
        self.assertEqual(json["aps"]["thread-id"], b64encode(self.comment.post_id))
        self.assertEqual(json["aps"]["relevance-score"], 0.0)
        self.assertEqual(json["aps"]["badge"], count_notifications_for(self.other_user))

    def test_make_json_clear(self):
        payload = apns.make_payload(None, "notifications:clear")
        json = apns.make_json(payload, None, self.main_user)
        self.assertEqual(json["aps"], {"content-available": 1})
        self.assertIsNone(json["_aps.relevance-score"])

    def test_filter_users(self):
        self.main_user.blocked_users.add(self.comment.author)
        users = list(apns.filter_users(self.comment, [self.main_user, self.other_user]))
        self.assertEqual([user.id for user in users], [self.other_user.id])


class ApnsMakeJwt(BaseCommentTestCase):
    def test_disabled(self):
        self.assertEqual(apns.make_jwt(), "")

    def test_enabled(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

        with override_settings(
            APNS_PRIVATE_KEY=pem,
            APNS_PRIVATE_KEY_ID="key-id",
            APPLE_TEAM_ID="team-id",
        ):
            token = apns.make_jwt()

        headers = jwt.get_unverified_header(token)
        self.assertEqual(headers["alg"], "ES256")
        self.assertEqual(headers["kid"], "key-id")
        claims = jwt.decode(token, key.public_key(), algorithms=["ES256"])
        self.assertEqual(claims["iss"], "team-id")
        self.assertIn("iat", claims)


class ApnsSendMessage(BaseCommentTestCase):
    def setUp(self):
        super().setUp()
        ApnsToken.objects.create(token="jwt")
        self.connection = Connection.objects.create(user=self.main_user)
        self.messaging = RemoteMessaging.objects.create(
            connection=self.connection, service=MessagingService.APNS, token="token"
        )

    def test_disabled(self):
        self.assertIsNone(
            apns.send_message(self.comment, [self.main_user], "comment:creation")
        )

    @override_settings(APNS_PRIVATE_KEY=b"key")
    def test_enabled(self):
        with mock.patch("notifications.remote.apns.Client") as client_class:
            client = client_class.return_value.__enter__.return_value
            response = mock.Mock(status_code=200)
            client.post.return_value = response

            apns.send_message(self.comment, [self.main_user], "comment:creation")

            client.post.assert_called_once()
            self.assertTrue(
                RemoteMessaging.objects.filter(id=self.messaging.id).exists()
            )
            response.raise_for_status.assert_called_once()

    @override_settings(APNS_PRIVATE_KEY=b"key")
    def test_not_found(self):
        with mock.patch("notifications.remote.apns.Client") as client_class:
            client = client_class.return_value.__enter__.return_value
            client.post.return_value = mock.Mock(status_code=404)

            apns.send_message(self.comment, [self.main_user], "comment:creation")

            self.assertFalse(
                RemoteMessaging.objects.filter(id=self.messaging.id).exists()
            )

    @override_settings(APNS_PRIVATE_KEY=b"key")
    def test_gone(self):
        with mock.patch("notifications.remote.apns.Client") as client_class:
            client = client_class.return_value.__enter__.return_value
            client.post.return_value = mock.Mock(status_code=410)

            apns.send_message(self.comment, [self.main_user], "comment:creation")

            self.assertFalse(
                RemoteMessaging.objects.filter(id=self.messaging.id).exists()
            )

    @override_settings(APNS_PRIVATE_KEY=b"key")
    def test_error(self):
        with mock.patch("notifications.remote.apns.Client") as client_class:
            client = client_class.return_value.__enter__.return_value
            response = mock.Mock(status_code=500)
            response.raise_for_status.side_effect = RuntimeError("boom")
            client.post.return_value = response

            with self.assertRaises(RuntimeError):
                apns.send_message(self.comment, [self.main_user], "comment:creation")
