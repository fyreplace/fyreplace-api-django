from unittest.case import TestCase

from django.test import override_settings
from jwt import InvalidTokenError

from . import jwt


class Jwt(TestCase):
    def test_round_trip(self):
        payload = {"user_id": "123", "timestamp": 456.789}
        self.assertEqual(jwt.decode(jwt.encode(payload)), payload)

    def test_garbage_token(self):
        with self.assertRaises(InvalidTokenError):
            jwt.decode("garbage")

    def test_other_key(self):
        token = jwt.encode({"user_id": "123"})

        with override_settings(SECRET_KEY="z" * 32):
            with self.assertRaises(InvalidTokenError):
                jwt.decode(token)
