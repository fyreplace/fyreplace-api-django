from django.contrib.auth import get_user_model
from grpc_interceptor.exceptions import InvalidArgument

from users.tests import BaseUserTestCase

from .utils import make_uuid


class BytesLookup(BaseUserTestCase):
    def test(self):
        self.main_user.refresh_from_db()
        user = get_user_model().objects.get(id__bytes=self.main_user.id.bytes)
        self.assertEqual(user.id, self.main_user.id)

    def test_filter(self):
        users = get_user_model().objects.filter(id__bytes=self.main_user.id.bytes)
        self.assertEqual(list(users), [self.main_user])

    def test_no_match(self):
        users = get_user_model().objects.filter(id__bytes=make_uuid(b"n" * 16).bytes)
        self.assertEqual(list(users), [])

    def test_invalid_bytes(self):
        with self.assertRaises(InvalidArgument):
            get_user_model().objects.filter(id__bytes=b"invalid")
