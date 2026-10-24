from unittest.case import TestCase
from uuid import uuid4

from grpc_interceptor.exceptions import InvalidArgument

from .utils import make_uuid, str_to_bool


class StrToBool(TestCase):
    def test_true(self):
        for value in ("True", "true", "TRUE"):
            self.assertTrue(str_to_bool(value))

    def test_false(self):
        for value in ("False", "false", "FALSE"):
            self.assertFalse(str_to_bool(value))

    def test_invalid(self):
        for value in ("", "yes", "no", "1", "0"):
            with self.assertRaises(ValueError):
                str_to_bool(value)


class MakeUuid(TestCase):
    def test(self):
        original = uuid4()
        self.assertEqual(make_uuid(original.bytes), original)

    def test_arbitrary_bytes(self):
        original = uuid4()
        data = original.bytes
        self.assertEqual(make_uuid(data).bytes, data)

    def test_too_short(self):
        with self.assertRaises(InvalidArgument):
            make_uuid(b"short")

    def test_too_long(self):
        with self.assertRaises(InvalidArgument):
            make_uuid(b"x" * 17)

    def test_empty(self):
        with self.assertRaises(InvalidArgument):
            make_uuid(b"")
