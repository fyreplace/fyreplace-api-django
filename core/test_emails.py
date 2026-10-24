from unittest.case import TestCase

from .emails import Email


class DummyEmail(Email):
    pass


class EmailTemplate(TestCase):
    def test(self):
        self.assertEqual(DummyEmail().template, "dummy")

    def test_multi_word(self):
        class AccountActivationEmail(Email):
            pass

        self.assertEqual(AccountActivationEmail().template, "account_activation")
