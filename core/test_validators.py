from types import SimpleNamespace
from unittest.case import TestCase

from grpc_interceptor.exceptions import InvalidArgument

from .validators import FileSizeValidator


class FileSizeValidatorTest(TestCase):
    def test_under_limit(self):
        FileSizeValidator(max_bytes=100)(SimpleNamespace(size=99))

    def test_at_limit(self):
        FileSizeValidator(max_bytes=100)(SimpleNamespace(size=100))

    def test_over_limit(self):
        with self.assertRaises(InvalidArgument):
            FileSizeValidator(max_bytes=100)(SimpleNamespace(size=101))
