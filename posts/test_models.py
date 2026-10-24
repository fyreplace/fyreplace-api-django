from datetime import datetime, timedelta, timezone
from time import sleep
from unittest.case import TestCase

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.images import ImageFile
from django.db import IntegrityError
from django.utils.timezone import now
from grpc_interceptor.exceptions import InvalidArgument, PermissionDenied

from core.tests import get_asset

from .models import (
    Chapter,
    Comment,
    Post,
    Stack,
    Subscription,
    Visibility,
    Vote,
    position_between,
)
from .tests import BaseCommentTestCase, BasePostTestCase, PublishedPostTestCase


class PositionBetween(TestCase):
    def test(self):
        for data in [
            (None, None, "z"),
            ("z", None, "zz"),
            (None, "z", "az"),
            ("z", "zz", "zaz"),
            ("z", "zaz", "zaaz"),
            ("zaz", "zz", "zazz"),
            ("az", "z", "azz"),
            ("azz", "z", "azzz"),
            ("az", "azz", "azaz"),
        ]:
            self.assertEqual(position_between(data[0], data[1]), data[2])

    def test_invalid_argument_type(self):
        with self.assertRaises(TypeError):
            position_between(42, None)

    def test_same_argument(self):
        with self.assertRaises(RuntimeError):
            position_between("z", "z")

    def test_inverted_argument(self):
        with self.assertRaises(RuntimeError):
            position_between("z", "az")


class User_delete(BaseCommentTestCase):
    def test(self):
        self.main_user.delete()
        self.assertEqual(Post.objects.filter(is_deleted=False).count(), 0)
        self.assertEqual(Comment.objects.filter(is_deleted=False).count(), 0)


class Post_delete(BasePostTestCase):
    def setUp(self):
        super().setUp()
        Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text=f"Text"
        )
        self.post.publish(anonymous=False)
        self.other_user.stack.fill()

    def test(self):
        self.assertEqual(self.other_user.stack.posts.count(), 1)
        self.post.delete()
        self.assertEqual(self.other_user.stack.posts.count(), 0)


class Post_normalize_chapters(BasePostTestCase):
    def test(self):
        for i in range(Post.MAX_CHAPTERS):
            Chapter.objects.create(
                post=self.post,
                position=self.post.chapter_position(i),
                text=f"Text {i}",
            )

        chapters = list(self.post.chapters.all())
        chapters[3].position = self.post.chapter_position(6)
        chapters[5].position = self.post.chapter_position(1)
        chapters[9].position = self.post.chapter_position(4)
        chapters = list(self.post.chapters.all())
        self.post.normalize_chapters()

        positions = [
            "aaaaaz",
            "aaaaz",
            "aaaz",
            "aaz",
            "az",
            "z",
            "zz",
            "zzz",
            "zzzz",
            "zzzzz",
        ]

        for i, chapter in enumerate(self.post.chapters.all()):
            self.assertEqual(chapter.id, chapters[i].id)
            self.assertEqual(chapter.position, positions[i])


class Chapter_validate(BasePostTestCase):
    def test_text(self):
        chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        self.assertValid(chapter)

    def test_image(self):
        chapter = Chapter.objects.create(
            post=self.post,
            position=self.post.chapter_position(0),
            image=self._get_image_file(),
        )

        self.assertEqual(chapter.width, 256)
        self.assertEqual(chapter.height, 256)
        self.assertValid(chapter)

    def test_empty(self):
        chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0)
        )

        with self.assertRaises(InvalidArgument):
            chapter.validate()

    def test_text_empty(self):
        chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text=""
        )

        with self.assertRaises(InvalidArgument):
            chapter.validate()

    def assertValid(self, chapter: Chapter):
        try:
            chapter.validate()
        except InvalidArgument as err:
            self.fail(err.detail)

    def _get_image_file(self):
        asset = open(get_asset("image.png"), "rb")
        return ImageFile(file=asset, name="image.png")


class Stack_fill(PublishedPostTestCase):
    def test(self):
        post_life = self.post.life
        before = now()
        sleep(0.1)
        stack = self.other_user.stack
        stack.fill()
        self.assertEqual(stack.posts.count(), 1)
        self.assertEqual(stack.posts.first().life, post_life - 1)
        self.assertGreater(stack.date_last_filled, before)

    def test_again(self):
        stack = self.other_user.stack
        stack.fill()
        self.assertEqual(stack.posts.count(), 1)
        stack.fill()
        self.assertEqual(stack.posts.count(), 1)

    def test_completely(self):
        for user in (self.main_user, self.other_user):
            for i in range(15):
                post = Post.objects.create(author=user)
                Chapter.objects.create(
                    post=post, position=self.post.chapter_position(0), text="Text"
                )
                post.publish(anonymous=False)

        stack = self.other_user.stack
        stack.fill()
        self.assertEqual(stack.posts.count(), 10)
        self.assertEqual(stack.posts.filter(author=self.other_user).count(), 0)


class Stack_drain(PublishedPostTestCase):
    def test(self):
        post_life = self.post.life
        stack = self.other_user.stack
        stack.fill()
        stack.drain()
        self.assertEqual(stack.posts.count(), 0)
        self.assertEqual(self.post.life, post_life)


class Vote_create(PublishedPostTestCase):
    def test_spread(self):
        self.other_user.stack.fill()
        self.post.refresh_from_db()
        post_life = self.post.life
        stack_count = self.other_user.stack.posts.count()
        Vote.objects.create(user=self.other_user, post=self.post, spread=True)
        self.post.refresh_from_db()
        self.assertEqual(
            self.post.life, post_life + settings.FYREPLACE_POST_SPREAD_LIFE
        )
        self.assertEqual(self.other_user.stack.posts.count(), stack_count - 1)

    def test_no_spread(self):
        self.other_user.stack.fill()
        self.post.refresh_from_db()
        post_life = self.post.life
        stack_count = self.other_user.stack.posts.count()
        Vote.objects.create(user=self.other_user, post=self.post, spread=False)
        self.post.refresh_from_db()
        self.assertEqual(self.post.life, post_life)
        self.assertEqual(self.other_user.stack.posts.count(), stack_count - 1)

    def test_same_user(self):
        with self.assertRaises(IntegrityError):
            Vote.objects.create(user=self.main_user, post=self.post, spread=True)

    def test_exising_vote(self):
        Vote.objects.create(user=self.other_user, post=self.post, spread=True)

        with self.assertRaises(IntegrityError):
            Vote.objects.create(user=self.other_user, post=self.post, spread=True)


class Comment_count(PublishedPostTestCase):
    def setUp(self):
        super().setUp()
        self.total = 10

    def test_before(self):
        comments = []

        for i in range(self.total):
            comments.append(
                Comment.objects.create(
                    post=self.post, author=self.other_user, text="Text"
                )
            )

        for i in range(self.total):
            self.assertEqual(comments[i].count(after=False), i)

    def test_after(self):
        comments = []

        for i in range(self.total):
            comments.append(
                Comment.objects.create(
                    post=self.post, author=self.other_user, text="Text"
                )
            )

        for i in range(self.total):
            self.assertEqual(comments[i].count(after=True), self.total - 1 - i)

    def test_count_deleted(self):
        comments = []

        for i in range(self.total):
            comments.append(
                Comment.objects.create(
                    post=self.post, author=self.other_user, text="Text"
                )
            )

        comments[5].delete()

        for i, comment in enumerate(comments):
            expected_before = i if i <= 5 else i - 1
            self.assertEqual(
                comment.count(after=False, count_deleted=False), expected_before
            )
            expected_after = self.total - 2 - i if i < 5 else self.total - 1 - i
            self.assertEqual(
                comment.count(after=True, count_deleted=False), expected_after
            )


class Comment_position(PublishedPostTestCase):
    def test(self):
        comments = []

        for i in range(3):
            comments.append(
                Comment.objects.create(
                    post=self.post, author=self.other_user, text="Text"
                )
            )

        for i, comment in enumerate(comments):
            self.assertEqual(comment.position, i)


class Post_str(BasePostTestCase):
    def test(self):
        self.assertIn(str(self.post.author), str(self.post))
        self.assertIn(str(self.post.chapters.count()), str(self.post))


class Chapter_str(BasePostTestCase):
    def test(self):
        chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        self.assertIn(str(self.post), str(chapter))
        self.assertIn(chapter.position, str(chapter))


class Stack_str(BasePostTestCase):
    def test(self):
        self.assertEqual(str(self.main_user.stack), str(self.main_user))


class Vote_str(PublishedPostTestCase):
    def test(self):
        vote = Vote.objects.create(user=self.other_user, post=self.post, spread=True)
        self.assertIn(str(self.other_user), str(vote))
        self.assertIn(str(self.post), str(vote))
        self.assertIn(str(vote.spread), str(vote))


class Visibility_str(PublishedPostTestCase):
    def test(self):
        self.other_user.stack.fill()
        visibility = Visibility.objects.first()
        self.assertIn(str(visibility.stack), str(visibility))
        self.assertIn(str(visibility.post), str(visibility))


class Comment_str(BaseCommentTestCase):
    def test(self):
        self.assertIn(str(self.comment.author), str(self.comment))
        self.assertIn(str(self.comment.post), str(self.comment))


class Subscription_str(PublishedPostTestCase):
    def test(self):
        subscription = Subscription.objects.create(user=self.other_user, post=self.post)
        self.assertIn(str(self.other_user), str(subscription))
        self.assertIn(str(self.post), str(subscription))


class Post_counts(PublishedPostTestCase):
    def test(self):
        self.assertEqual(self.post.vote_count, 0)
        self.assertEqual(self.post.comment_count, 0)
        Vote.objects.create(user=self.other_user, post=self.post, spread=True)

        for _ in range(3):
            Comment.objects.create(post=self.post, author=self.other_user, text="Text")

        self.post.refresh_from_db()
        self.assertEqual(self.post.vote_count, 1)
        self.assertEqual(self.post.comment_count, 3)
        Comment.objects.filter(post=self.post).first().delete()
        self.assertEqual(self.post.comment_count, 2)


class Post_convert_field(BasePostTestCase):
    def test_draft(self):
        message = self.post.to_message()
        self.assertAlmostEqual(
            datetime.fromtimestamp(message.date_created.seconds, tz=timezone.utc),
            self.post.date_created,
            delta=timedelta(seconds=1),
        )

    def test_published(self):
        Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        self.post.publish(anonymous=False)
        message = self.post.to_message()
        self.assertAlmostEqual(
            datetime.fromtimestamp(message.date_created.seconds, tz=timezone.utc),
            self.post.date_published,
            delta=timedelta(seconds=1),
        )


class Post_overrides_for_user(BasePostTestCase):
    def setUp(self):
        super().setUp()
        Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        self.post.publish(anonymous=False)

    def test_subscribed(self):
        self.assertEqual(self.post.overrides_for_user(self.other_user), {})
        self.post.subscribers.add(self.other_user)
        self.assertEqual(
            self.post.overrides_for_user(self.other_user), {"is_subscribed": True}
        )
        self.assertEqual(
            self.post.overrides_for_user(self.main_user), {"is_subscribed": True}
        )

    def test_comments_read(self):
        subscription = Subscription.objects.create(user=self.other_user, post=self.post)
        comment = Comment.objects.create(
            post=self.post, author=self.main_user, text="Text"
        )
        subscription.last_comment_seen = comment
        subscription.save()
        overrides = self.post.overrides_for_user(self.other_user)
        self.assertEqual(overrides["comments_read"], 1)

    def test_anonymous(self):
        self.post.is_anonymous = True
        self.post.save()
        self.assertEqual(
            self.post.overrides_for_user(self.other_user), {"author": None}
        )
        self.assertEqual(
            self.post.overrides_for_user(self.main_user), {"is_subscribed": True}
        )
        self.assertEqual(self.post.overrides_for_user(None), {"author": None})


class Post_queryset(BasePostTestCase):
    def setUp(self):
        super().setUp()
        self.own_draft = Post.objects.create(author=self.main_user)
        self.other_draft = Post.objects.create(author=self.other_user)
        self.other_published = Post.objects.create(author=self.other_user)
        Chapter.objects.create(
            post=self.other_published,
            position=self.other_published.chapter_position(0),
            text="Text",
        )
        self.other_published.publish(anonymous=False)

    def test_get_readable_by(self):
        posts = Post.existing_objects
        self.assertEqual(
            posts.get_readable_by(self.main_user, id=self.own_draft.id).id,
            self.own_draft.id,
        )
        self.assertEqual(
            posts.get_readable_by(self.main_user, id=self.other_published.id).id,
            self.other_published.id,
        )

        with self.assertRaises(ObjectDoesNotExist):
            posts.get_readable_by(self.main_user, id=self.other_draft.id)

    def test_get_writable_by(self):
        self.assertEqual(
            Post.existing_objects.get_writable_by(
                self.main_user, id=self.own_draft.id
            ).id,
            self.own_draft.id,
        )

        with self.assertRaises(PermissionDenied):
            Post.existing_objects.get_writable_by(
                self.main_user, id=self.other_published.id
            )

        with self.assertRaises(ObjectDoesNotExist):
            Post.existing_objects.get_writable_by(
                self.main_user, id=self.other_draft.id
            )

    def test_get_published_readable_by(self):
        self.assertEqual(
            Post.existing_objects.get_published_readable_by(
                self.main_user, id=self.other_published.id
            ).id,
            self.other_published.id,
        )

        with self.assertRaises(PermissionDenied):
            Post.existing_objects.get_published_readable_by(
                self.main_user, id=self.own_draft.id
            )

        with self.assertRaises(ObjectDoesNotExist):
            Post.existing_objects.get_published_readable_by(
                self.main_user, id=self.other_draft.id
            )


class Stack_fill_exclusions(PublishedPostTestCase):
    def test_full(self):
        for i in range(Stack.MAX_SIZE):
            post = Post.objects.create(author=self.main_user)
            Chapter.objects.create(
                post=post, position=post.chapter_position(0), text="Text"
            )
            post.publish(anonymous=False)

        stack = self.other_user.stack
        stack.fill()
        self.assertEqual(stack.posts.count(), Stack.MAX_SIZE)
        date_last_filled = stack.date_last_filled
        stack.fill()
        self.assertEqual(stack.posts.count(), Stack.MAX_SIZE)

    def test_blocking_users(self):
        self.main_user.blocked_users.add(self.other_user)
        stack = self.other_user.stack
        stack.fill()
        self.assertEqual(stack.posts.filter(author=self.main_user).count(), 0)

    def test_voters(self):
        Vote.objects.create(user=self.other_user, post=self.post, spread=True)
        stack = self.other_user.stack
        stack.fill()
        self.assertEqual(stack.posts.filter(id=self.post.id).count(), 0)


class Post_soft_delete_life(PublishedPostTestCase):
    def test(self):
        self.assertGreater(self.post.life, 0)
        self.post.delete()
        self.post.refresh_from_db()
        self.assertEqual(self.post.life, 0)


class Post_chapter_position(BasePostTestCase):
    def test(self):
        self.assertEqual(self.post.chapter_position(0), "z")

        for i in range(1, 4):
            Chapter.objects.create(
                post=self.post, position=self.post.chapter_position(i - 1), text="Text"
            )
            self.assertEqual(self.post.chapter_position(i), "z" * (i + 1))

    def test_invalid(self):
        for position in (-1, 1, 42):
            with self.assertRaises(InvalidArgument):
                self.post.chapter_position(position)


class Post_get_chapter_at(BasePostTestCase):
    def setUp(self):
        super().setUp()
        self.chapters = [
            Chapter.objects.create(
                post=self.post, position=self.post.chapter_position(i), text="Text"
            )
            for i in range(3)
        ]

    def test(self):
        for i, chapter in enumerate(self.chapters):
            self.assertEqual(self.post.get_chapter_at(i).id, chapter.id)

    def test_for_update(self):
        self.assertEqual(
            self.post.get_chapter_at(0, for_update=True).id, self.chapters[0].id
        )

    def test_invalid(self):
        with self.assertRaises(InvalidArgument):
            self.post.get_chapter_at(3)


class Chapter_clear_save(BasePostTestCase):
    def test_with_image(self):
        chapter = Chapter.objects.create(
            post=self.post,
            position=self.post.chapter_position(0),
            image=ImageFile(open(get_asset("image.png"), "rb"), name="image.png"),
        )
        chapter.clear()
        chapter.refresh_from_db()
        self.assertEqual(chapter.text, "")
        self.assertFalse(chapter.is_title)
        self.assertFalse(chapter.image)

    def test_without_image(self):
        chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        chapter.clear()
        chapter.refresh_from_db()
        self.assertEqual(chapter.text, "")
        self.assertFalse(chapter.is_title)
        self.assertFalse(chapter.image)
