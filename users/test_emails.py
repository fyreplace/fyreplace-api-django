from base64 import b64decode
from unittest.case import TestCase
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from core import jwt
from protos import user_pb2_grpc

from .emails import (
    AccountActivationEmail,
    AccountConnectionEmail,
    UserBannedEmail,
    UserEmailUpdateEmail,
    deep_link,
    qr_data,
)
from .tests import BaseUserTestCase, make_email


class DeepLink(TestCase):
    def test_http(self):
        link = deep_link(user_pb2_grpc.AccountService.ConfirmActivation)
        self.assertEqual(
            link,
            f"https://{settings.EMAIL_LINKS_DOMAIN}/AccountService.ConfirmActivation",
        )

    def test_app(self):
        link = deep_link(user_pb2_grpc.AccountService.ConfirmActivation, http=False)
        self.assertEqual(link, "fyreplace:///AccountService.ConfirmActivation")


class QrData(TestCase):
    def test(self):
        data = b64decode(qr_data("https://example.org"))
        self.assertEqual(data[:4], b"\x89PNG")


class AccountActivationEmailTests(BaseUserTestCase):
    def test_subject(self):
        email = AccountActivationEmail(self.main_user.id)
        self.assertEqual(email.subject, "Fyreplace account activation")

    def test_template(self):
        email = AccountActivationEmail(self.main_user.id)
        self.assertEqual(email.template, "account_activation")

    def test_recipients(self):
        email = AccountActivationEmail(self.main_user.id)
        self.assertEqual(email.recipients, [make_email("main")])

    def test_token(self):
        email = AccountActivationEmail(self.main_user.id)
        claims = jwt.decode(email.token)
        self.assertEqual(claims["user_id"], str(self.main_user.id))
        self.assertIn("timestamp", claims)
        self.assertNotIn("connection_token", claims)

    def test_context(self):
        email = AccountActivationEmail(self.main_user.id)
        context = email.context
        self.assertTrue(context["link"].startswith("https://"))
        self.assertIn("AccountService.ConfirmActivation", context["link"])
        self.assertIn("#", context["link"])

    def test_non_existent_user(self):
        with self.assertRaises(ObjectDoesNotExist):
            AccountActivationEmail(uuid4())


class AccountConnectionEmailTests(BaseUserTestCase):
    def test_subject(self):
        email = AccountConnectionEmail(self.main_user.id)
        self.assertEqual(email.subject, "Fyreplace account connection")

    def test_connection_token(self):
        self.main_user.connection_token = uuid4()
        self.main_user.save()
        email = AccountConnectionEmail(self.main_user.id)
        claims = jwt.decode(email.token)
        self.assertEqual(
            claims["connection_token"], str(self.main_user.connection_token)
        )


class UserEmailUpdateEmailTests(BaseUserTestCase):
    def test_subject(self):
        email = UserEmailUpdateEmail(self.main_user.id, make_email("new"))
        self.assertEqual(email.subject, "Fyreplace account email confirmation")

    def test_recipients(self):
        email = UserEmailUpdateEmail(self.main_user.id, make_email("new"))
        self.assertEqual(email.recipients, [make_email("new")])

    def test_token(self):
        email = UserEmailUpdateEmail(self.main_user.id, make_email("new"))
        claims = jwt.decode(email.token)
        self.assertEqual(claims["user_id"], str(self.main_user.id))
        self.assertEqual(claims["email"], make_email("new"))


class UserBannedEmailTests(BaseUserTestCase):
    def test_subject(self):
        email = UserBannedEmail(self.main_user.id)
        self.assertEqual(email.subject, "Fyreplace ban notice")

    def test_recipients(self):
        email = UserBannedEmail(self.main_user.id)
        self.assertEqual(email.recipients, [make_email("main")])

    def test_context(self):
        from datetime import timedelta

        self.main_user.ban(timedelta(days=3))
        email = UserBannedEmail(self.main_user.id)
        self.assertEqual(email.context["date_ban_end"], self.main_user.date_ban_end)
