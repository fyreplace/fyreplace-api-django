from django.db import IntegrityError
from django.test import TestCase

from posts.models import Comment
from posts.tests import BaseCommentTestCase

from .models import CachedRequest
from .signals import post_soft_delete, pre_soft_delete


class SoftDeleteModel(BaseCommentTestCase):
    def test(self):
        received = []

        def pre_receiver(sender, instance, **kwargs):
            received.append("pre")

        def post_receiver(sender, instance, **kwargs):
            received.append("post")

        pre_soft_delete.connect(pre_receiver)
        post_soft_delete.connect(post_receiver)
        self.addCleanup(pre_soft_delete.disconnect, pre_receiver)
        self.addCleanup(post_soft_delete.disconnect, post_receiver)

        self.comment.soft_delete()
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)
        self.assertEqual(received, ["pre", "post"])


class ExistingManager(BaseCommentTestCase):
    def setUp(self):
        super().setUp()
        Comment.objects.create(post=self.post, author=self.other_user, text="Other")

    def test(self):
        comments = Comment.objects.filter(post=self.post)
        self.assertEqual(comments.count(), 2)
        self.comment.delete()
        self.assertEqual(Comment.objects.filter(post=self.post).count(), 2)
        self.assertEqual(Comment.existing_objects.filter(post=self.post).count(), 1)


class CachedRequestTestCase(TestCase):
    def test_unique_request_id(self):
        CachedRequest.objects.create(
            request_id="id",
            serialized_response="{}",
            serialized_response_message=b"x",
        )

        with self.assertRaises(IntegrityError):
            CachedRequest.objects.create(
                request_id="id",
                serialized_response="{}",
                serialized_response_message=b"x",
            )
