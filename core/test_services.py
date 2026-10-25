from types import SimpleNamespace
from typing import Iterator
from unittest.case import TestCase

from django.contrib.auth import get_user_model
from django.core.files.images import ImageFile
from django.test import TestCase
from google.protobuf import empty_pb2
from grpc_interceptor.exceptions import InvalidArgument

from core.tests import get_asset
from protos import image_pb2

from .services import ImageUploadMixin, get_servicer_interfaces


class TestUploader(ImageUploadMixin):
    def make_request(
        self, name: str, total_size: int = None
    ) -> Iterator[image_pb2.ImageChunk]:
        with open(get_asset(name), "rb") as image:
            data = image.read()

        if total_size is not None:
            data = data.ljust(total_size, b"\x00")

        while data:
            yield image_pb2.ImageChunk(data=data[:64])
            data = data[64:]


class GetServicerInterfaces(TestCase):
    def test_servicers(self):
        from notifications.services import NotificationService
        from posts.services import ChapterService, CommentService, PostService
        from users.services import AccountService, UserService

        for service, servicer_name in [
            (AccountService, "AccountServiceServicer"),
            (UserService, "UserServiceServicer"),
            (PostService, "PostServiceServicer"),
            (ChapterService, "ChapterServiceServicer"),
            (CommentService, "CommentServiceServicer"),
            (NotificationService, "NotificationServiceServicer"),
        ]:
            servicers = get_servicer_interfaces(service)
            self.assertEqual(len(servicers), 1)
            self.assertEqual(servicers[0].__name__, servicer_name)

    def test_instance(self):
        from users.services import AccountService

        self.assertEqual(get_servicer_interfaces(AccountService()), [])

    def test_non_servicer(self):
        self.assertEqual(get_servicer_interfaces(object()), [])
        self.assertEqual(get_servicer_interfaces(empty_pb2.Empty), [])


class GetImage(TestCase):
    def setUp(self):
        self.uploader = TestUploader()

    def test_jpeg(self):
        image = self.uploader.get_image(self.uploader.make_request("image.jpeg"))
        self.assertTrue(image.name.endswith(".jpeg"))

    def test_png(self):
        image = self.uploader.get_image(self.uploader.make_request("image.png"))
        self.assertTrue(image.name.endswith(".png"))

    def test_webp(self):
        image = self.uploader.get_image(self.uploader.make_request("image.webp"))
        self.assertTrue(image.name.endswith(".webp"))

    def test_empty(self):
        self.assertIsNone(self.uploader.get_image(iter([])))

    def test_bad_format(self):
        with self.assertRaises(InvalidArgument):
            self.uploader.get_image(self.uploader.make_request("image.txt"))

    def test_bad_format_early(self):
        request = self.uploader.make_request("image.txt", total_size=4096)

        with self.assertRaises(InvalidArgument):
            self.uploader.get_image(request)

    def test_payload_too_large(self):
        request = self.uploader.make_request("image.jpeg", total_size=1024 * 1024 + 1)

        with self.assertRaises(InvalidArgument):
            self.uploader.get_image(request)

    def test_chunkator(self):
        data = open(get_asset("image.jpeg"), "rb").read()
        requests = [SimpleNamespace(chunk=image_pb2.ImageChunk(data=data))]
        image = self.uploader.get_image(iter(requests), chunkator=lambda u: u.chunk)
        self.assertTrue(image.name.endswith(".jpeg"))


class SetImage(TestCase):
    def setUp(self):
        self.uploader = ImageUploadMixin()
        self.user = get_user_model().objects.create_user(
            username="image", email="image@fyreplace.app"
        )

    def tearDown(self):
        self.user.avatar.delete(save=False)
        self.user.delete()

    def test(self):
        image = ImageFile(open(get_asset("image.jpeg"), "rb"), name="image.jpeg")
        self.uploader.set_image(self.user, "avatar", image)
        self.user.refresh_from_db()
        self.assertTrue(self.user.avatar.name.endswith(".jpeg"))

    def test_replacement_deletes_old_file(self):
        image = ImageFile(open(get_asset("image.jpeg"), "rb"), name="image.jpeg")
        self.uploader.set_image(self.user, "avatar", image)
        self.user.refresh_from_db()
        old_name = self.user.avatar.name

        new_image = ImageFile(open(get_asset("image.png"), "rb"), name="image.png")
        self.uploader.set_image(self.user, "avatar", new_image)
        self.user.refresh_from_db()

        self.assertNotEqual(self.user.avatar.name, old_name)
        self.assertFalse(self.user.avatar.storage.exists(old_name))
