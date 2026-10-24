from types import SimpleNamespace
from unittest.case import TestCase
from uuid import UUID

from django.conf import settings
from django.test import override_settings

from .storages import FileSystemStorage, get_image_url


class GetImageUrl(TestCase):
    def test_absolute_url(self):
        url = "https://cdn.example.org/image.jpeg"
        self.assertEqual(get_image_url(SimpleNamespace(url=url)), url)

    @override_settings(BASE_URL="https://api.fyreplace.app")
    def test_relative_url(self):
        url = "/media/image.jpeg"
        self.assertEqual(
            get_image_url(SimpleNamespace(url=url)),
            f"{settings.BASE_URL}{url}",
        )

    @override_settings(BASE_URL=None)
    def test_no_base_url(self):
        url = "/media/image.jpeg"
        self.assertEqual(get_image_url(SimpleNamespace(url=url)), url)


class FileSystemStorageName(TestCase):
    def test(self):
        storage = FileSystemStorage()
        name = storage.get_available_name("avatars/image.jpeg")
        dir_name, file_name = name.rsplit("/", 1)
        _, file_ext = file_name.rsplit(".", 1)
        self.assertEqual(dir_name, "avatars")
        UUID(file_name.removesuffix(f".{file_ext}"))
        self.assertEqual(file_ext, "jpeg")

    def test_unique(self):
        storage = FileSystemStorage()
        name_1 = storage.get_available_name("avatars/image.jpeg")
        name_2 = storage.get_available_name("avatars/image.jpeg")
        self.assertNotEqual(name_1, name_2)
