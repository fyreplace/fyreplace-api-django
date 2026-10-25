from unittest.case import TestCase

from .authentication import no_auth


class NoAuth(TestCase):
    def test(self):
        @no_auth
        def method():
            pass

        self.assertTrue(method.__dict__["no_auth"])

    def test_without_decorator(self):
        def method():
            pass

        self.assertNotIn("no_auth", method.__dict__)
