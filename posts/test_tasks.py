from datetime import timedelta
from uuid import uuid4

from django.utils.timezone import now

from .models import Chapter, Comment, Post, Stack, Visibility
from .tasks import cleanup_stacks, remove_post_data, remove_post_data_for_user
from .tests import BasePostTestCase, PublishedPostTestCase


class Task_cleanup_stacks(PublishedPostTestCase):
    def setUp(self):
        super().setUp()
        self.stack = Stack.objects.get(user=self.other_user)
        self.stack.fill()

    def test(self):
        self.assertEqual(self.stack.posts.count(), 1)
        Stack.objects.filter(user=self.other_user).update(
            date_last_filled=now() - timedelta(days=1)
        )
        cleanup_stacks.delay()
        self.assertEqual(self.stack.posts.count(), 0)

    def test_too_soon(self):
        self.assertEqual(self.stack.posts.count(), 1)
        cleanup_stacks.delay()
        self.assertEqual(self.stack.posts.count(), 1)

    def test_no_posts(self):
        self.stack.drain()
        Stack.objects.filter(user=self.other_user).update(
            date_last_filled=now() - timedelta(days=1)
        )
        cleanup_stacks.delay()
        self.assertEqual(self.stack.posts.count(), 0)


class Task_remove_post_data_for_user(PublishedPostTestCase):
    def test_posts(self):
        self.assertEqual(
            Post.objects.filter(author=self.main_user, is_deleted=False).count(), 1
        )
        remove_post_data_for_user.delay(user_id=str(self.main_user.id))
        self.assertEqual(
            Post.objects.filter(author=self.main_user, is_deleted=False).count(),
            0,
        )

    def test_comments(self):
        Comment.objects.create(post=self.post, author=self.main_user, text="Text")
        remove_post_data_for_user.delay(user_id=str(self.main_user.id))
        self.assertEqual(
            Comment.objects.filter(is_deleted=False, author=self.main_user).count(),
            0,
        )

    def test_non_existent_user(self):
        post_count = Post.objects.filter(is_deleted=False).count()
        comment_count = Comment.objects.filter(is_deleted=False).count()
        remove_post_data_for_user.delay(user_id=str(uuid4()))
        self.assertEqual(Post.objects.filter(is_deleted=False).count(), post_count)
        self.assertEqual(
            Comment.objects.filter(is_deleted=False).count(), comment_count
        )


class Task_remove_post_data(PublishedPostTestCase):
    def setUp(self):
        super().setUp()
        self.other_stack = Stack.objects.get(user=self.other_user)
        self.other_stack.fill()

    def _soft_delete_without_signals(self):
        Post.objects.filter(id=self.post.id).update(is_deleted=True)

    def test(self):
        self._soft_delete_without_signals()
        self.assertEqual(self.post.chapters.count(), 1)
        self.assertEqual(Visibility.objects.filter(post_id=self.post.id).count(), 1)
        remove_post_data.delay(post_id=str(self.post.id))
        self.assertEqual(Chapter.objects.filter(post_id=self.post.id).count(), 0)
        self.assertEqual(Visibility.objects.filter(post_id=self.post.id).count(), 0)

    def test_visibility_covers_all_deleted_posts(self):
        other_post = Post.objects.create(author=self.main_user)
        Chapter.objects.create(
            post=other_post, position=other_post.chapter_position(0), text="Text"
        )
        other_post.publish(anonymous=False)
        self._soft_delete_without_signals()
        self.assertEqual(Visibility.objects.filter(post_id=self.post.id).count(), 1)

        remove_post_data.delay(post_id=str(other_post.id))
        self.assertEqual(Visibility.objects.filter(post_id=self.post.id).count(), 0)

    def test_non_existent_post(self):
        chapter_count = Chapter.objects.count()
        comment_count = Comment.objects.count()
        visibility_count = Visibility.objects.count()
        remove_post_data.delay(post_id=str(uuid4()))
        self.assertEqual(Chapter.objects.count(), chapter_count)
        self.assertEqual(Comment.objects.count(), comment_count)
        self.assertEqual(Visibility.objects.count(), visibility_count)
