import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.images import ImageFile
from django.utils.timezone import now
from google.protobuf.message import Message
from grpc_interceptor.exceptions import InvalidArgument, PermissionDenied

from core.tests import ImageTestCaseMixin, PaginationTestCase, get_asset
from notifications.models import Flag, Notification
from notifications.tests import BaseNotificationTestCase
from protos import comment_pb2, id_pb2, image_pb2, pagination_pb2, post_pb2
from users.models import User
from users.tests import AuthenticatedTestCase

from .models import Chapter, Comment, Post, Subscription, Vote
from .services import ChapterService, CommentService, PostService


class PostServiceTestCase(AuthenticatedTestCase, BaseNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.service = PostService()

    def _create_posts(
        self,
        author: User,
        count: int,
        published: bool,
        anonymous: bool = False,
    ) -> list[Post]:
        posts = []

        for _ in range(count):
            post = Post.objects.create(author=author)

            for position in range(3):
                text = f"Text {post.id}/{position}"
                Chapter.objects.create(
                    post=post, position=post.chapter_position(position), text=text
                )

            if published:
                post.publish(anonymous=anonymous)

            posts.append(post)

        return posts


class PostPaginationTestCase(PostServiceTestCase, PaginationTestCase):
    def setUp(self):
        super().setUp()
        self.posts = self._create_test_posts()
        self.out_of_bounds_cursor = pagination_pb2.Cursor(
            data=[
                pagination_pb2.KeyValuePair(
                    key=self.main_pagination_field, value=str(now())
                ),
                pagination_pb2.KeyValuePair(key="id", value=str(self.posts[-1].id)),
            ],
            is_next=True,
        )

    def paginate(
        self, request_iterator: Iterator[pagination_pb2.Page]
    ) -> Iterator[Message]:
        raise NotImplementedError

    def check(self, item: post_pb2.Post, position: int):
        post = self.posts[position]
        self.assertEqual(item.id, post.id.bytes)
        self.assertTrue(item.is_preview)
        self.assertEqual(len(item.chapters), 1)
        self.assertEqual(item.chapter_count, post.chapters.count())

    def run_test_deleted_posts(self):
        for post in self.posts[-6:]:
            post.delete()

        self.posts = self.posts[:-6]
        page_requests = self.get_initial_requests(forward=True)
        posts_iterator = self.paginate(page_requests)
        posts = next(posts_iterator)
        self.assertEqual(len(posts.posts), self.page_size)

        for i, post in enumerate(posts.posts):
            self.assertEqual(post.id, self.posts[i].id.bytes)

        page_requests.append(pagination_pb2.Page(cursor=posts.next))
        posts = next(posts_iterator)
        self.assertCursorEmpty(posts.next)
        self.assertEqual(len(posts.posts), self.page_size - 6)

        for i, post in enumerate(posts.posts):
            self.assertEqual(post.id, self.posts[i + self.page_size].id.bytes)

    def _create_test_posts(self) -> list[Post]:
        raise NotImplementedError


class ChapterServiceTestCase(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.service = ChapterService()
        self.post = Post.objects.create(author=self.main_user)


class CommentServiceTestCase(AuthenticatedTestCase, BaseNotificationTestCase):
    def setUp(self):
        super().setUp()
        self.service = CommentService()
        self.post = Post.objects.create(author=self.other_user)
        Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        self.post.publish(anonymous=False)

    def _create_comments(
        self,
        author: User,
        count: int,
    ) -> list[Post]:
        return [
            Comment.objects.create(post=self.post, author=author, text="Text")
            for _ in range(count)
        ]


class PostService_ListFeed(PostServiceTestCase):
    def test(self):
        posts = self._create_some_posts()
        feed = self.service.ListFeed(
            iter(self._create_requests(posts)), self.grpc_context
        )

        for post in posts:
            next_post = next(feed)
            chapters = post.chapters.all()
            self.assertEqual(next_post.id, post.id.bytes)
            self.assertFalse(next_post.is_preview)
            self.assertEqual(len(next_post.chapters), len(chapters))

            for i, chapter in enumerate(next_post.chapters):
                self.assertEqual(chapter.text, chapters[i].text)

    def test_anonymous(self):
        self.grpc_context.set_user(None)
        posts = self._create_some_posts(include_main_user=True)
        feed = self.service.ListFeed(
            iter(self._create_requests(posts)), self.grpc_context
        )

        for post in posts:
            next_post = next(feed)
            chapters = post.chapters.all()
            self.assertEqual(next_post.id, post.id.bytes)
            self.assertFalse(next_post.is_preview)
            self.assertEqual(len(next_post.chapters), len(chapters))

            for i, chapter in enumerate(next_post.chapters):
                self.assertEqual(chapter.text, chapters[i].text)

        for post in Post.active_objects.all():
            self.assertEqual(post.life, 10)

    def test_empty(self):
        feed = self.service.ListFeed([], self.grpc_context)
        self.assertEqual(list(feed), [])

    def test_deleted_posts(self):
        posts = self._create_some_posts()
        active_posts = []

        for i, post in enumerate(posts):
            if i % 2:
                post.delete()
            else:
                active_posts.append(post)

        requests = self._create_requests(active_posts)
        feed = self.service.ListFeed(iter(requests), self.grpc_context)

        for post in active_posts:
            self.assertEqual(next(feed).id, post.id.bytes)

    def test_old_posts(self):
        posts = self._create_some_posts()
        active_posts = []

        for i, post in enumerate(posts):
            if i % 2:
                post.date_published -= settings.FYREPLACE_POST_MAX_DURATION
                post.save()
            else:
                active_posts.append(post)

        requests = self._create_requests(active_posts)
        feed = self.service.ListFeed(iter(requests), self.grpc_context)

        for post in active_posts:
            self.assertEqual(next(feed).id, post.id.bytes)

    def test_dead_posts(self):
        posts = self._create_some_posts()
        alive_posts = []

        for i, post in enumerate(posts):
            if i % 2:
                post.life = 0
                post.save()
            else:
                alive_posts.append(post)

        requests = self._create_requests(alive_posts)
        feed = self.service.ListFeed(iter(requests), self.grpc_context)

        for post in alive_posts:
            self.assertEqual(next(feed).id, post.id.bytes)

    def test_drafts(self):
        posts = self._create_some_posts()
        published_posts = []

        for i, post in enumerate(posts):
            if i % 2:
                post.date_published = None
                post.save()
            else:
                published_posts.append(post)

        requests = self._create_requests(published_posts)
        feed = self.service.ListFeed(iter(requests), self.grpc_context)

        for post in published_posts:
            self.assertEqual(next(feed).id, post.id.bytes)

    def test_blocked_user(self):
        self.main_user.blocked_users.add(self.other_user)
        posts = self._create_some_posts()
        feed = self.service.ListFeed(
            iter(self._create_requests(posts)), self.grpc_context
        )
        self.assertEqual(len(list(feed)), 0)

    def test_post_not_in_feed(self):
        posts = self._create_some_posts()
        requests = [
            post_pb2.Vote(post_id=posts[0].id.bytes, spread=True),
            post_pb2.Vote(post_id=posts[5].id.bytes, spread=True),
        ]
        feed = self.service.ListFeed(iter(requests), self.grpc_context)

        for _ in range(4):
            next(feed)

        with self.assertRaises(InvalidArgument):
            next(feed)

    def _create_some_posts(self, include_main_user: bool = False) -> list[Post]:
        posts = self._create_posts(author=self.other_user, count=15, published=True)
        main_posts = self._create_posts(author=self.main_user, count=5, published=True)

        if include_main_user:
            posts += main_posts

        return posts + self._create_posts(
            author=self.other_user, count=10, published=True
        )

    def _create_requests(self, posts: list[Post]) -> list[post_pb2.Vote]:
        return [
            post_pb2.Vote(post_id=p.id.bytes, spread=i % 2 == 0)
            for i, p in enumerate(posts)
        ]


class PostService_ListArchive(PostPaginationTestCase):
    main_pagination_field = "date_last_seen"

    def setUp(self):
        super().setUp()
        self.out_of_bounds_cursor = pagination_pb2.Cursor(
            data=[
                pagination_pb2.KeyValuePair(key="date_last_seen", value=str(now())),
                pagination_pb2.KeyValuePair(
                    key="post_id", value=str(self.posts[-1].id)
                ),
            ],
            is_next=True,
        )

    def _create_test_posts(self) -> list[Post]:
        self._create_posts(author=self.main_user, count=5, published=False)
        posts = self._create_posts(author=self.other_user, count=10, published=True)
        posts += self._create_posts(author=self.main_user, count=8, published=True)
        posts += self._create_posts(author=self.other_user, count=10, published=True)
        the_posts = []

        for i, post in enumerate(posts):
            if i % 8:
                post.subscribers.add(self.main_user)
                the_posts.append(post)
            else:
                post.subscribers.remove(self.main_user)

        the_posts = sorted(
            the_posts,
            key=lambda p: p.subscriptions.get(user=self.main_user).date_last_seen,
        )
        return the_posts

    def paginate(
        self, request_iterator: Iterator[pagination_pb2.Page]
    ) -> Iterator[Message]:
        return self.service.ListArchive(request_iterator, self.grpc_context)

    def test(self):
        self.run_test(self.check)

    def test_previous(self):
        self.run_test_previous(self.check)

    def test_reverse(self):
        self.run_test_reverse(self.check)

    def test_reverse_previous(self):
        self.run_test_reverse_previous(self.check)

    def test_empty(self):
        self.run_test_empty(Post.published_objects.all())

    def test_invalid_size(self):
        self.run_test_invalid_size()

    def test_no_header(self):
        self.run_test_no_header()

    def test_out_of_bounds(self):
        self.run_test_out_of_bounds(self.out_of_bounds_cursor)

    def test_deleted_posts(self):
        self.run_test_deleted_posts()


class PostService_ListOwnPosts(PostPaginationTestCase):
    main_pagination_field = "date_published"

    def _create_test_posts(self) -> list[Post]:
        self._create_posts(author=self.main_user, count=5, published=False)
        the_posts = self._create_posts(author=self.main_user, count=14, published=True)
        self._create_posts(author=self.other_user, count=8, published=True)
        the_posts += self._create_posts(author=self.main_user, count=10, published=True)
        the_posts = sorted(the_posts, key=lambda p: p.date_published)
        return the_posts

    def paginate(
        self, request_iterator: Iterator[pagination_pb2.Page]
    ) -> Iterator[Message]:
        return self.service.ListOwnPosts(request_iterator, self.grpc_context)

    def test(self):
        self.run_test(self.check)

    def test_previous(self):
        self.run_test_previous(self.check)

    def test_reverse(self):
        self.run_test_reverse(self.check)

    def test_reverse_previous(self):
        self.run_test_reverse_previous(self.check)

    def test_empty(self):
        self.run_test_empty(Post.published_objects.filter(author=self.main_user))

    def test_invalid_size(self):
        self.run_test_invalid_size()

    def test_no_header(self):
        self.run_test_no_header()

    def test_out_of_bounds(self):
        self.run_test_out_of_bounds(self.out_of_bounds_cursor)

    def test_deleted_posts(self):
        self.run_test_deleted_posts()


class PostService_ListDrafts(PostPaginationTestCase):
    def _create_test_posts(self) -> list[Post]:
        self._create_posts(author=self.main_user, count=5, published=True)
        the_posts = self._create_posts(author=self.main_user, count=14, published=False)
        self._create_posts(author=self.other_user, count=8, published=False)
        the_posts += self._create_posts(
            author=self.main_user, count=10, published=False
        )
        the_posts = sorted(the_posts, key=lambda p: p.date_created)
        return the_posts

    def paginate(
        self, request_iterator: Iterator[pagination_pb2.Page]
    ) -> Iterator[Message]:
        return self.service.ListDrafts(request_iterator, self.grpc_context)

    def test(self):
        self.run_test(self.check)

    def test_previous(self):
        self.run_test_previous(self.check)

    def test_reverse(self):
        self.run_test_reverse(self.check)

    def test_reverse_previous(self):
        self.run_test_reverse_previous(self.check)

    def test_empty(self):
        self.run_test_empty(Post.draft_objects.filter(author=self.main_user))

    def test_invalid_size(self):
        self.run_test_invalid_size()

    def test_no_header(self):
        self.run_test_no_header()

    def test_out_of_bounds(self):
        self.run_test_out_of_bounds(self.out_of_bounds_cursor)

    def test_deleted_posts(self):
        self.run_test_deleted_posts()


class PostService_Retrieve(PostServiceTestCase):
    def test(self):
        request = self._get_request(author=self.main_user, published=True)
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertFalse(post.is_preview)
        self.assertEqual(post.author.id, self.main_user.id.bytes)
        chapters = self.post.chapters.all()

        for i, chapter in enumerate(post.chapters):
            self.assertEqual(chapter.text, chapters[i].text)

    def test_anonymous(self):
        request = self._get_request(
            author=self.main_user, published=True, anonymous=True
        )
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertEqual(post.author.id, self.main_user.id.bytes)

    def test_draft(self):
        request = self._get_request(author=self.main_user, published=False)
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)

    def test_deleted(self):
        request = self._get_request(author=self.main_user, published=True)
        self.post.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Retrieve(request, self.grpc_context)

    def test_other(self):
        request = self._get_request(author=self.other_user, published=True)
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertFalse(post.is_preview)
        self.assertEqual(post.author.id, self.other_user.id.bytes)
        chapters = self.post.chapters.all()

        for i, chapter in enumerate(post.chapters):
            self.assertEqual(chapter.text, chapters[i].text)

    def test_other_anonymous(self):
        request = self._get_request(
            author=self.other_user, published=True, anonymous=True
        )
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertEqual(post.author.id, b"")

    def test_other_draft(self):
        request = self._get_request(author=self.other_user, published=False)

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Retrieve(request, self.grpc_context)

    def test_comment(self):
        request = self._get_comment_request(author=self.main_user, published=True)
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertFalse(post.is_preview)
        self.assertEqual(post.author.id, self.main_user.id.bytes)

    def test_comment_anonymous(self):
        request = self._get_comment_request(
            author=self.main_user, published=True, anonymous=True
        )
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertEqual(post.author.id, self.main_user.id.bytes)

    def test_comment_deleted(self):
        request = self._get_comment_request(author=self.main_user, published=True)
        self.post.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Retrieve(request, self.grpc_context)

    def test_comment_other(self):
        request = self._get_request(author=self.other_user, published=True)
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertFalse(post.is_preview)
        self.assertEqual(post.author.id, self.other_user.id.bytes)

    def test_comment_other_anonymous(self):
        request = self._get_request(
            author=self.other_user, published=True, anonymous=True
        )
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.id, self.post.id.bytes)
        self.assertEqual(post.author.id, b"")

    def test_non_existent(self):
        request = id_pb2.Id(id=uuid.uuid4().bytes)

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Retrieve(request, self.grpc_context)

    def test_date_last_seen(self):
        request = self._get_request(author=self.main_user, published=True)
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        Subscription.objects.filter(id=subscription.id).update(
            date_last_seen=now() - timedelta(days=1)
        )
        before = Subscription.objects.get(id=subscription.id).date_last_seen
        self.service.Retrieve(request, self.grpc_context)
        subscription.refresh_from_db()
        self.assertGreater(subscription.date_last_seen, before)

    def test_vote_and_comment_counts(self):
        request = self._get_request(author=self.main_user, published=True)
        Vote.objects.create(user=self.other_user, post=self.post, spread=True)
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertEqual(post.vote_count, 1)
        self.assertEqual(post.comment_count, 1)

    def test_date_created(self):
        request = self._get_request(author=self.main_user, published=True)
        post = self.service.Retrieve(request, self.grpc_context)
        self.assertAlmostEqual(
            datetime.fromtimestamp(post.date_created.seconds, tz=timezone.utc),
            self.post.date_published,
            delta=timedelta(seconds=1),
        )

    def _get_request(
        self, author: User, published: bool, anonymous: bool = False
    ) -> id_pb2.Id:
        posts = self._create_posts(
            author=author, count=1, published=published, anonymous=anonymous
        )
        self.post = posts[0]
        return id_pb2.Id(id=self.post.id.bytes)

    def _get_comment_request(
        self, author: User, published: bool, anonymous: bool = False
    ):
        self._get_request(author=author, published=published, anonymous=anonymous)
        self.comment = Comment.objects.create(
            post=self.post, author=self.other_user, text="Text"
        )
        return id_pb2.Id(id=self.comment.id.bytes)


class PostService_Create(PostServiceTestCase):
    def test(self):
        post_id = self.service.Create(self.request, self.grpc_context)
        post = Post.objects.latest("date_created")
        self.assertEqual(post_id.id, post.id.bytes)


class PostService_Publish(PostServiceTestCase):
    def setUp(self):
        super().setUp()
        posts = self._create_posts(author=self.main_user, count=1, published=False)
        self.post = posts[0]
        self.request = post_pb2.Publication(id=self.post.id.bytes, anonymous=False)

    def test(self):
        date = now()
        self.service.Publish(self.request, self.grpc_context)
        self.post.refresh_from_db()
        self.assertFalse(self.post.is_anonymous)
        self.assertEqual(self.post.life, 10)
        self.assertAlmostEqual(
            self.post.date_published, date, delta=timedelta(seconds=1)
        )
        self.assertEqual([self.main_user], list(self.post.subscribers.all()))

    def test_anonymous(self):
        date = now()
        self.request.anonymous = True
        self.service.Publish(self.request, self.grpc_context)
        self.post.refresh_from_db()
        self.assertTrue(self.post.is_anonymous)
        self.assertAlmostEqual(
            self.post.date_published, date, delta=timedelta(seconds=1)
        )
        self.assertEqual([self.main_user], list(self.post.subscribers.all()))

    def test_empty(self):
        self.post.chapters.all().delete()

        with self.assertRaises(InvalidArgument):
            self.service.Publish(self.request, self.grpc_context)

    def test_chapter_empty(self):
        chapter = self.post.chapters.first()
        chapter.text = ""
        chapter.save()

        with self.assertRaises(InvalidArgument):
            self.service.Publish(self.request, self.grpc_context)

    def test_already_published(self):
        self.post.publish(anonymous=False)

        with self.assertRaises(PermissionDenied):
            self.service.Publish(self.request, self.grpc_context)

    def test_deleted(self):
        self.post.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Publish(self.request, self.grpc_context)

    def test_other(self):
        self.post.author = self.other_user
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Publish(self.request, self.grpc_context)

    def test_other_already_published(self):
        self.post.author = self.other_user
        self.post.publish(anonymous=False)

        with self.assertRaises(PermissionDenied):
            self.service.Publish(self.request, self.grpc_context)


class PostService_Delete(PostServiceTestCase):
    def test(self):
        post = self._create_posts(author=self.main_user, count=1, published=True)[0]
        request = id_pb2.Id(id=post.id.bytes)
        self.service.Delete(request, self.grpc_context)
        post.refresh_from_db()
        self.assertTrue(post.is_deleted)
        self.assertEqual(post.chapters.count(), 0)

    def test_anonymous(self):
        post = self._create_posts(
            author=self.main_user, count=1, published=True, anonymous=True
        )[0]
        request = id_pb2.Id(id=post.id.bytes)
        self.service.Delete(request, self.grpc_context)
        post.refresh_from_db()
        self.assertTrue(post.is_deleted)

    def test_is_staff(self):
        post = self._create_posts(author=self.other_user, count=1, published=True)[0]
        self.main_user.is_staff = True
        self.main_user.save()
        request = id_pb2.Id(id=post.id.bytes)
        self.service.Delete(request, self.grpc_context)
        post.refresh_from_db()
        self.assertTrue(post.is_deleted)

    def test_draft(self):
        post = self._create_posts(author=self.main_user, count=1, published=False)[0]
        request = id_pb2.Id(id=post.id.bytes)
        self.service.Delete(request, self.grpc_context)

        with self.assertRaises(ObjectDoesNotExist):
            Post.existing_objects.get(id=post.id)

    def test_deleted(self):
        post = self._create_posts(author=self.main_user, count=1, published=True)[0]
        post.delete()
        request = id_pb2.Id(id=post.id.bytes)

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Delete(request, self.grpc_context)

    def test_other(self):
        post = self._create_posts(author=self.other_user, count=1, published=True)[0]
        request = id_pb2.Id(id=post.id.bytes)

        with self.assertRaises(PermissionDenied):
            self.service.Delete(request, self.grpc_context)

    def test_other_anonymous(self):
        post = self._create_posts(author=self.other_user, count=1, published=True)[0]
        request = id_pb2.Id(id=post.id.bytes)

        with self.assertRaises(PermissionDenied):
            self.service.Delete(request, self.grpc_context)

    def test_other_draft(self):
        post = self._create_posts(author=self.other_user, count=1, published=False)[0]
        request = id_pb2.Id(id=post.id.bytes)

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Delete(request, self.grpc_context)


class PostService_UpdateSubscription(PostServiceTestCase):
    def setUp(self):
        super().setUp()
        posts = self._create_posts(author=self.other_user, count=1, published=True)
        self.post = posts[0]
        self.request = post_pb2.Subscription(id=self.post.id.bytes)

    def test(self):
        self.request.subscribed = True
        comment = Comment.objects.create(
            post=self.post, author=self.other_user, text="Text"
        )
        self.service.UpdateSubscription(self.request, self.grpc_context)
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        self.assertIn(self.main_user, self.post.subscribers.all())
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        self.assertEqual(subscription.last_comment_seen, comment)

    def test_unsubscribe(self):
        self.request.subscribed = False
        self.service.UpdateSubscription(self.request, self.grpc_context)
        self.assertNotIn(self.main_user, self.post.subscribers.all())

    def test_draft(self):
        post = self._create_posts(author=self.other_user, count=1, published=False)[0]
        self.request.id = post.id.bytes
        self.request.subscribed = True

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateSubscription(self.request, self.grpc_context)

    def test_self_draft(self):
        post = self._create_posts(author=self.main_user, count=1, published=False)[0]
        self.request.id = post.id.bytes
        self.request.subscribed = True

        with self.assertRaises(PermissionDenied):
            self.service.UpdateSubscription(self.request, self.grpc_context)

    def test_deleted(self):
        self.post.delete()
        self.request.subscribed = True

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateSubscription(self.request, self.grpc_context)

    def test_subscribe_no_comments(self):
        self.request.subscribed = True
        self.service.UpdateSubscription(self.request, self.grpc_context)
        self.assertIn(self.main_user, self.post.subscribers.all())
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        self.assertIsNone(subscription.last_comment_seen)

    def test_resubscribe(self):
        self.request.subscribed = True
        comment = Comment.objects.create(
            post=self.post, author=self.other_user, text="Text"
        )
        self.service.UpdateSubscription(self.request, self.grpc_context)
        Comment.objects.create(post=self.post, author=self.other_user, text="Text")
        self.service.UpdateSubscription(self.request, self.grpc_context)
        self.assertEqual(
            Subscription.objects.filter(user=self.main_user, post=self.post).count(),
            1,
        )
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        self.assertEqual(subscription.last_comment_seen, comment)

    def test_unsubscribe_subscribed(self):
        self.post.subscribers.add(self.main_user)
        self.request.subscribed = False
        self.service.UpdateSubscription(self.request, self.grpc_context)
        self.assertNotIn(self.main_user, self.post.subscribers.all())

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes
        self.request.subscribed = True

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateSubscription(self.request, self.grpc_context)


class PostService_Report(PostServiceTestCase):
    def setUp(self):
        super().setUp()
        posts = self._create_posts(author=self.other_user, count=1, published=True)
        self.post = posts[0]
        self.request = id_pb2.Id(id=self.post.id.bytes)

    def test(self):
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(flag.target_type, ContentType.objects.get_for_model(Post))
        self.assertEqual(flag.target_id, str(self.post.id))

    def test_draft(self):
        self.post.date_published = None
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Report(self.request, self.grpc_context)

    def test_deleted(self):
        self.post.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Report(self.request, self.grpc_context)

    def test_self_author(self):
        posts = self._create_posts(author=self.main_user, count=1, published=True)
        self.post = posts[0]
        self.request = id_pb2.Id(id=self.post.id.bytes)

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_twice(self):
        self.service.Report(self.request, self.grpc_context)
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Report(self.request, self.grpc_context)


class PostService_Absolve(PostServiceTestCase):
    def setUp(self):
        super().setUp()
        posts = self._create_posts(author=self.other_user, count=1, published=True)
        self.post = posts[0]
        Flag.objects.create(issuer=self.main_user, target=self.post)
        self.main_user.is_staff = True
        self.main_user.save()
        self.request = id_pb2.Id(id=self.post.id.bytes)

    def test(self):
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(Post, self.post.id)

    def test_empty(self):
        Notification.flag_objects.all().delete()
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(Post, self.post.id)

    def test_not_staff(self):
        self.main_user.is_staff = False
        self.main_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_on_own(self):
        self.post.author = self.main_user
        self.post.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_on_draft(self):
        self.post.date_published = None
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Absolve(self.request, self.grpc_context)

    def test_deleted(self):
        self.post.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Absolve(self.request, self.grpc_context)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Absolve(self.request, self.grpc_context)


class ChapterService_Create(ChapterServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = post_pb2.ChapterLocation(post_id=self.post.id.bytes, position=0)

    def test(self):
        self.service.Create(self.request, self.grpc_context)
        self.assertEqual(self.post.chapters.count(), 1)
        self.assertEqual(self.post.chapters.first().position, "z")

    def test_after_chapter(self):
        first = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0)
        )
        self.request.position = 1
        self.service.Create(self.request, self.grpc_context)
        first.refresh_from_db()
        self.assertEqual(first.position, "z")
        self.assertEqual(self.post.chapters.count(), 2)
        self.assertEqual(self.post.chapters.last().position, "zz")

    def test_between_chapters(self):
        first = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0)
        )
        last = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(1)
        )
        self.request.position = 1
        self.service.Create(self.request, self.grpc_context)
        first.refresh_from_db()
        last.refresh_from_db()
        self.assertEqual(first.position, "z")
        self.assertEqual(last.position, "zz")
        self.assertEqual(self.post.chapters.count(), 3)
        self.assertEqual(self.post.chapters.all()[1].position, "zaz")

    def test_invalid_position(self):
        self.request.position = 4

        with self.assertRaises(InvalidArgument):
            self.service.Create(self.request, self.grpc_context)

    def test_too_many(self):
        for i in range(Post.MAX_CHAPTERS):
            self.request.position = i
            self.service.Create(self.request, self.grpc_context)

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

    def test_published(self):
        Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0), text="Text"
        )
        self.post.publish(anonymous=False)
        self.request.position = 1

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

    def test_other(self):
        post = Post.objects.create(author=self.other_user)
        self.request.post_id = post.id.bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Create(self.request, self.grpc_context)

    def test_non_existent_post(self):
        self.request.post_id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Create(self.request, self.grpc_context)

    def test_deleted_post(self):
        post = Post.objects.create(author=self.main_user)
        post_id = post.id.bytes
        post.delete()
        self.request.post_id = post_id

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Create(self.request, self.grpc_context)


class ChapterService_Move(ChapterServiceTestCase):
    def setUp(self):
        super().setUp()
        self.chapters = [
            Chapter.objects.create(
                post=self.post, position=self.post.chapter_position(i), text="Text"
            )
            for i in range(4)
        ]
        self.request = post_pb2.ChapterRelocation(
            post_id=self.post.id.bytes, from_position=2, to_position=1
        )

    def test(self):
        self.service.Move(self.request, self.grpc_context)
        self.assertChapters([0, 2, 1, 3])

    def test_same_position(self):
        self.request.to_position = self.request.from_position
        self.service.Move(self.request, self.grpc_context)
        self.assertChapters([0, 1, 2, 3])

    def test_to_start(self):
        self.request.to_position = 0
        self.service.Move(self.request, self.grpc_context)
        self.assertChapters([2, 0, 1, 3])

    def test_to_end(self):
        self.request.to_position = 3
        self.service.Move(self.request, self.grpc_context)
        self.assertChapters([0, 1, 3, 2])

    def test_from_invalid_position(self):
        with self.assertRaises(ValueError):
            self.request.from_position = -1

        with self.assertRaises(InvalidArgument):
            self.request.from_position = 42
            self.service.Move(self.request, self.grpc_context)

    def test_to_invalid_position(self):
        with self.assertRaises(ValueError):
            self.request.to_position = -1

        with self.assertRaises(InvalidArgument):
            self.request.to_position = 42
            self.service.Move(self.request, self.grpc_context)

    def test_published(self):
        self.post.publish(anonymous=False)

        with self.assertRaises(PermissionDenied):
            self.service.Move(self.request, self.grpc_context)

    def test_other(self):
        self.post.author = self.other_user
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Move(self.request, self.grpc_context)

    def test_non_existent_post(self):
        self.request.post_id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Move(self.request, self.grpc_context)

    def assertChapters(self, order: list[int]):
        for i, position in enumerate(order):
            self.assertEqual(self.post.chapters.all()[i].id, self.chapters[position].id)


class ChapterService_UpdateText(ChapterServiceTestCase):
    def setUp(self):
        super().setUp()
        self.chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0)
        )
        location = post_pb2.ChapterLocation(post_id=self.post.id.bytes, position=0)
        self.request = post_pb2.ChapterTextUpdate(location=location, text="Text")

    def test(self):
        self.service.UpdateText(self.request, self.grpc_context)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.text, self.request.text)
        self.assertFalse(self.chapter.is_title)

    def test_title(self):
        self.request.is_title = True
        self.service.UpdateText(self.request, self.grpc_context)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.text, self.request.text)
        self.assertTrue(self.chapter.is_title)

    def test_empty(self):
        self.request.text = ""
        self.service.UpdateText(self.request, self.grpc_context)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.text, self.request.text)

    def test_has_content(self):
        asset = open(get_asset("image.jpeg"), "rb")
        self.chapter.image = ImageFile(asset, name=f"image.jpeg")
        self.chapter.save()
        self.service.UpdateText(self.request, self.grpc_context)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.text, self.request.text)
        self.assertEqual(str(self.chapter.image), "")

    def test_invalid_position(self):
        self.request.location.position = 42

        with self.assertRaises(InvalidArgument):
            self.service.UpdateText(self.request, self.grpc_context)

    def test_too_long(self):
        self.request.text = "a" * (Chapter.text.field.max_length + 1)

        with self.assertRaises(ValidationError):
            self.service.UpdateText(self.request, self.grpc_context)

    def test_published(self):
        self.chapter.text = "Text"
        self.chapter.save()
        self.post.publish(anonymous=False)

        with self.assertRaises(PermissionDenied):
            self.service.UpdateText(self.request, self.grpc_context)

    def test_other(self):
        self.post.author = self.other_user
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateText(self.request, self.grpc_context)

    def test_non_existent_post(self):
        self.request.location.post_id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateText(self.request, self.grpc_context)


class ChapterService_UpdateImage(ImageTestCaseMixin, ChapterServiceTestCase):
    def setUp(self):
        super().setUp()
        self.chapter = Chapter.objects.create(
            post=self.post, position=self.post.chapter_position(0)
        )

    def test(self):
        image = self.service.UpdateImage(
            self.make_update_request("jpeg"), self.grpc_context
        )
        self.chapter.refresh_from_db()
        regex = r".*\." + "jpeg"
        self.assertRegex(str(self.chapter.image), regex)
        self.assertEqual(self.chapter.width, 256)
        self.assertEqual(self.chapter.height, 256)
        self.assertRegex(image.url, regex)
        self.assertEqual(image.width, 256)
        self.assertEqual(image.height, 256)

    def test_empty(self):
        location = post_pb2.ChapterLocation(post_id=self.post.id.bytes, position=0)
        request = [post_pb2.ChapterImageUpdate(location=location)]
        self.service.UpdateImage(iter(request), self.grpc_context)
        self.chapter.refresh_from_db()
        self.assertEqual(str(self.chapter.image), "")

    def test_has_content(self):
        self.chapter.text = "Text"
        self.chapter.save()
        self.service.UpdateImage(self.make_update_request("jpeg"), self.grpc_context)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.text, "")
        self.assertRegex(str(self.chapter.image), r".*\." + "jpeg")

    def test_invalid_position(self):
        with self.assertRaises(InvalidArgument):
            self.service.UpdateImage(
                self.make_update_request("jpeg", position=42), self.grpc_context
            )

    def test_missing_location(self):
        with self.assertRaises(InvalidArgument):
            self.service.UpdateImage(iter([]), self.grpc_context)

    def test_published(self):
        asset = open(get_asset("image.jpeg"), "rb")
        self.chapter.image = ImageFile(asset, name=f"image.jpeg")
        self.chapter.save()
        self.post.publish(anonymous=False)

        with self.assertRaises(PermissionDenied):
            self.service.UpdateImage(
                self.make_update_request("jpeg"), self.grpc_context
            )

    def test_other(self):
        self.post.author = self.other_user
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.UpdateImage(
                self.make_update_request("jpeg"), self.grpc_context
            )

    def test_bad_format(self):
        with self.assertRaises(InvalidArgument):
            self.service.UpdateImage(self.make_update_request("txt"), self.grpc_context)

    def test_payload_too_large(self):
        with open(get_asset("image.jpeg"), "rb") as asset:
            data = asset.read()
        data = data.ljust(512 * 1024 + 1, b"\x00")
        location = post_pb2.ChapterLocation(post_id=self.post.id.bytes, position=0)
        request = iter(
            [
                post_pb2.ChapterImageUpdate(location=location),
                post_pb2.ChapterImageUpdate(chunk=image_pb2.ImageChunk(data=data)),
            ]
        )

        with self.assertRaises(InvalidArgument):
            self.service.UpdateImage(request, self.grpc_context)

    def test_png(self):
        image = self.service.UpdateImage(
            self.make_update_request("png"), self.grpc_context
        )
        self.chapter.refresh_from_db()
        self.assertRegex(str(self.chapter.image), r".*\.png")
        self.assertRegex(image.url, r".*\.png")

    def test_replacement_deletes_old_file(self):
        self.service.UpdateImage(self.make_update_request("jpeg"), self.grpc_context)
        self.chapter.refresh_from_db()
        old_name = self.chapter.image.name

        self.service.UpdateImage(self.make_update_request("png"), self.grpc_context)
        self.chapter.refresh_from_db()

        self.assertNotEqual(self.chapter.image.name, old_name)
        self.assertFalse(self.chapter.image.storage.exists(old_name))

    def make_update_request(
        self, extension: str, position: int = 0
    ) -> Iterator[post_pb2.ChapterImageUpdate]:
        location = post_pb2.ChapterLocation(
            post_id=self.post.id.bytes, position=position
        )
        yield post_pb2.ChapterImageUpdate(location=location)

        for chunk in self.make_request(extension):
            yield post_pb2.ChapterImageUpdate(chunk=chunk)


class ChapterService_Delete(ChapterServiceTestCase):
    def setUp(self):
        super().setUp()
        self.chapters = [
            Chapter.objects.create(
                post=self.post, position=self.post.chapter_position(i), text="Text"
            )
            for i in range(3)
        ]
        self.request = post_pb2.ChapterLocation(post_id=self.post.id.bytes, position=1)

    def test(self):
        self.service.Delete(self.request, self.grpc_context)
        self.chapters.pop(self.request.position)
        self.assertEqual(
            list(self.post.chapters.values_list("id", flat=True)),
            [c.id for c in self.chapters],
        )

    def test_invalid_position(self):
        self.request.position = 42

        with self.assertRaises(InvalidArgument):
            self.service.Delete(self.request, self.grpc_context)

    def test_published(self):
        self.post.publish(anonymous=False)

        with self.assertRaises(PermissionDenied):
            self.service.Delete(self.request, self.grpc_context)

    def test_other(self):
        self.post.author = self.other_user
        self.post.save()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Delete(self.request, self.grpc_context)

    def test_non_existent_post(self):
        self.request.post_id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Delete(self.request, self.grpc_context)


class CommentService_List(CommentServiceTestCase, PaginationTestCase):
    def setUp(self):
        super().setUp()
        self.comments = self._create_test_comments()
        self.out_of_bounds_cursor = pagination_pb2.Cursor(
            data=[
                pagination_pb2.KeyValuePair(
                    key=self.main_pagination_field, value=str(now())
                ),
                pagination_pb2.KeyValuePair(key="id", value=str(self.comments[-1].id)),
            ],
            is_next=True,
        )

    def get_context_id(self) -> Optional[bytes]:
        return self.post.id.bytes

    def get_initial_requests(
        self, forward: bool, size: Optional[int] = None
    ) -> list[pagination_pb2.Page]:
        return [
            pagination_pb2.Page(
                header=pagination_pb2.Header(
                    forward=forward,
                    size=size if size is not None else self.page_size,
                    context_id=self.get_context_id(),
                )
            ),
            pagination_pb2.Page(offset=0),
        ]

    def paginate(
        self, request_iterator: Iterator[pagination_pb2.Page]
    ) -> Iterator[Message]:
        return self.service.List(request_iterator, self.grpc_context)

    def check(self, item: comment_pb2.Comment, position: int):
        self.assertEqual(item.id, self.comments[position].id.bytes)
        self.assertEqual(item.position, 0)
        self.assertEqual(item.text, self.comments[position].text)
        self.assertEqual(item.author.id, self.comments[position].author.id.bytes)

    def test(self):
        self.run_test(self.check, offset=True)

    def test_previous(self):
        self.run_test_previous(self.check, offset=True)

    def test_reverse(self):
        self.run_test_reverse(self.check, offset=True)

    def test_reverse_previous(self):
        self.run_test_reverse_previous(self.check, offset=True)

    def test_empty(self):
        self.run_test_empty(self.post.comments.all())

    def test_invalid_size(self):
        self.run_test_invalid_size()

    def test_no_header(self):
        self.run_test_no_header()

    def test_out_of_bounds(self):
        self.run_test_out_of_bounds(self.out_of_bounds_cursor)

    def test_deleted_comments(self):
        for comment in self.comments[-6:]:
            comment.delete()

        page_requests = self.get_initial_requests(forward=True)
        items_iterator = self.paginate(page_requests)
        comments = next(items_iterator)
        self.assertEqual(len(comments.comments), self.page_size)

        for i, comment in enumerate(comments.comments):
            self_comment = self.comments[i]
            self.assertEqual(comment.id, self_comment.id.bytes)
            self.assertEqual(comment.text, self_comment.text)

        page_requests.append(pagination_pb2.Page(offset=self.page_size))
        comments = next(items_iterator)
        self.assertEqual(len(comments.comments), self.page_size)

        for i, comment in enumerate(comments.comments):
            self_comment = self.comments[i + self.page_size]
            self.assertEqual(comment.id, self_comment.id.bytes)
            self.assertEqual(
                comment.text, "" if self_comment.is_deleted else self_comment.text
            )

    def test_other_banned_forever(self):
        self.other_user.ban()
        page_requests = self.get_initial_requests(forward=True)
        items_iterator = self.paginate(page_requests)
        comments = next(items_iterator)
        comment = comments.comments[-1]
        self.assertEqual(comment.author.id, self.other_user.id.bytes)
        self.assertEqual(comment.author.username, "")
        self.assertTrue(comment.author.is_banned)

    def test_anonymous(self):
        self.grpc_context.set_user(None)
        page_requests = self.get_initial_requests(forward=True)
        items_iterator = self.paginate(page_requests)
        comments = next(items_iterator)
        self.assertEqual(len(comments.comments), self.page_size)

    def test_non_existent_post(self):
        page_requests = self.get_initial_requests(forward=True)
        page_requests[0].header.context_id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            list(self.paginate(page_requests))

    def test_invalid_context_id(self):
        page_requests = self.get_initial_requests(forward=True)
        page_requests[0].header.context_id = b"invalid"

        with self.assertRaises(InvalidArgument):
            list(self.paginate(page_requests))

    def test_draft_post(self):
        draft = Post.objects.create(author=self.main_user)
        Chapter.objects.create(
            post=draft, position=draft.chapter_position(0), text="Text"
        )
        page_requests = self.get_initial_requests(forward=True)
        page_requests[0].header.context_id = draft.id.bytes

        with self.assertRaises(PermissionDenied):
            list(self.paginate(page_requests))

    def test_other_draft_post(self):
        draft = Post.objects.create(author=self.other_user)
        Chapter.objects.create(
            post=draft, position=draft.chapter_position(0), text="Text"
        )
        page_requests = self.get_initial_requests(forward=True)
        page_requests[0].header.context_id = draft.id.bytes

        with self.assertRaises(ObjectDoesNotExist):
            list(self.paginate(page_requests))

    def test_other_post_comments(self):
        other_post = Post.objects.create(author=self.other_user)
        Chapter.objects.create(
            post=other_post, position=other_post.chapter_position(0), text="Text"
        )
        other_post.publish(anonymous=False)
        other_comments = [
            Comment.objects.create(post=other_post, author=self.main_user, text="Text")
            for _ in range(3)
        ]
        page_requests = self.get_initial_requests(forward=True)
        items_iterator = self.paginate(page_requests)
        comments = next(items_iterator)
        self.assertEqual(len(comments.comments), self.page_size)
        ids = [c.id for c in comments.comments]
        for comment in other_comments:
            self.assertNotIn(comment.id.bytes, ids)

    def _create_test_comments(self) -> list[Comment]:
        comments = self._create_comments(author=self.main_user, count=10)
        comments += self._create_comments(author=self.other_user, count=4)
        comments += self._create_comments(author=self.main_user, count=10)
        return comments


class CommentService_Create(CommentServiceTestCase):
    def setUp(self):
        super().setUp()
        self.request = comment_pb2.CommentCreation(
            post_id=self.post.id.bytes, text="Text"
        )

    def test(self):
        comment_count = self.post.comments.count()
        comment_id = self.service.Create(self.request, self.grpc_context)
        self.assertEqual(self.post.comments.count(), comment_count + 1)
        comment = self.post.comments.latest("date_created")
        self.assertEqual(comment_id.id, comment.id.bytes)
        self.assertEqual(comment.text, self.request.text)
        self.assertIn(self.main_user, self.post.subscribers.all())
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        self.assertEqual(subscription.last_comment_seen, comment)

    def test_empty(self):
        self.request.text = ""
        comment_count = self.post.comments.count()

        with self.assertRaises(InvalidArgument):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(self.post.comments.count(), comment_count)

    def test_too_long(self):
        self.request.text = "a" * (Comment.text.field.max_length + 1)
        comment_count = self.post.comments.count()

        with self.assertRaises(ValidationError):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(self.post.comments.count(), comment_count)

    def test_draft(self):
        self.post.author = self.main_user
        self.post.date_published = None
        self.post.save()
        comment_count = self.post.comments.count()

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(self.post.comments.count(), comment_count)

    def test_other_draft(self):
        self.post.date_published = None
        self.post.save()
        comment_count = self.post.comments.count()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(self.post.comments.count(), comment_count)

    def test_blocked(self):
        self.other_user.blocked_users.add(self.main_user)
        comment_count = self.post.comments.count()

        with self.assertRaises(PermissionDenied):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(self.post.comments.count(), comment_count)

    def test_non_existent_post(self):
        self.request.post_id = uuid.uuid4().bytes
        comment_count = self.post.comments.count()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(self.post.comments.count(), comment_count)

    def test_deleted_post(self):
        post = Post.objects.create(author=self.other_user)
        Chapter.objects.create(
            post=post, position=post.chapter_position(0), text="Text"
        )
        post.publish(anonymous=False)
        post.delete()
        self.request.post_id = post.id.bytes
        comment_count = Comment.objects.count()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Create(self.request, self.grpc_context)

        self.assertEqual(Comment.objects.count(), comment_count)


class CommentService_Delete(CommentServiceTestCase):
    def setUp(self):
        super().setUp()
        self.comment = Comment.objects.create(
            post=self.post, author=self.main_user, text="Text"
        )
        self.request = id_pb2.Id(id=self.comment.id.bytes)

    def test(self):
        self.service.Delete(self.request, self.grpc_context)
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)
        self.assertEqual(self.comment.text, "")

    def test_is_staff(self):
        self.main_user.is_staff = True
        self.main_user.save()
        self.comment.author = self.other_user
        self.comment.save()
        self.service.Delete(self.request, self.grpc_context)
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)
        self.assertEqual(self.comment.text, "")

    def test_other(self):
        self.comment.author = self.other_user
        self.comment.save()

        with self.assertRaises(PermissionDenied):
            self.service.Delete(self.request, self.grpc_context)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Delete(self.request, self.grpc_context)

    def test_already_deleted(self):
        self.service.Delete(self.request, self.grpc_context)
        self.service.Delete(self.request, self.grpc_context)
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)
        self.assertEqual(self.comment.text, "")


class CommentService_Acknowledge(CommentServiceTestCase):
    def setUp(self):
        super().setUp()
        self.post.subscribers.add(self.main_user)
        self.comments = self._create_comments(author=self.other_user, count=10)
        self.comment = self.comments[5]
        self.request = id_pb2.Id(id=self.comment.id.bytes)

    def test(self):
        self.service.Acknowledge(self.request, self.grpc_context)
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        self.assertEqual(subscription.last_comment_seen, self.comment)
        notification = Notification.objects.get(subscription=subscription)
        self.assertEqual(notification.count, 4)

    def test_regress(self):
        self.service.Acknowledge(self.request, self.grpc_context)
        self.request.id = self.comments[3].id.bytes
        self.service.Acknowledge(self.request, self.grpc_context)
        subscription = Subscription.objects.get(user=self.main_user, post=self.post)
        self.assertEqual(subscription.last_comment_seen, self.comment)
        notification = Notification.objects.get(subscription=subscription)
        self.assertEqual(notification.count, 4)

    def test_not_subscribed(self):
        self.post.subscribers.remove(self.main_user)
        notification_count = Notification.objects.count()
        self.service.Acknowledge(self.request, self.grpc_context)
        subscription = Subscription.objects.filter(user=self.main_user, post=self.post)
        self.assertFalse(subscription.exists())
        self.assertEqual(Notification.objects.count(), notification_count)


class CommentService_Report(CommentServiceTestCase):
    def setUp(self):
        super().setUp()
        comments = self._create_comments(author=self.other_user, count=1)
        self.comment = comments[0]
        self.request = id_pb2.Id(id=self.comment.id.bytes)

    def test(self):
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)
        flag = Notification.flag_objects.first()
        self.assertEqual(flag.target_type, ContentType.objects.get_for_model(Comment))
        self.assertEqual(flag.target_id, str(self.comment.id))

    def test_self_author(self):
        comments = self._create_comments(author=self.main_user, count=1)
        self.comment = comments[0]
        self.request = id_pb2.Id(id=self.comment.id.bytes)

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_deleted(self):
        self.comment.delete()

        with self.assertRaises(PermissionDenied):
            self.service.Report(self.request, self.grpc_context)

    def test_twice(self):
        self.service.Report(self.request, self.grpc_context)
        self.service.Report(self.request, self.grpc_context)
        self.assertEqual(Notification.flag_objects.count(), 1)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Report(self.request, self.grpc_context)


class CommentService_Absolve(CommentServiceTestCase):
    def setUp(self):
        super().setUp()
        comments = self._create_comments(author=self.other_user, count=1)
        self.comment = comments[0]
        Flag.objects.create(issuer=self.main_user, target=self.comment)
        self.main_user.is_staff = True
        self.main_user.save()
        self.request = id_pb2.Id(id=self.comment.id.bytes)

    def test(self):
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(Comment, self.comment.id)

    def test_empty(self):
        Notification.flag_objects.all().delete()
        self.service.Absolve(self.request, self.grpc_context)
        self.assertNoFlags(Comment, self.comment.id)

    def test_not_staff(self):
        self.main_user.is_staff = False
        self.main_user.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_on_own(self):
        self.comment.author = self.main_user
        self.comment.save()

        with self.assertRaises(PermissionDenied):
            self.service.Absolve(self.request, self.grpc_context)

    def test_deleted(self):
        self.comment.delete()

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Absolve(self.request, self.grpc_context)

    def test_non_existent(self):
        self.request.id = uuid.uuid4().bytes

        with self.assertRaises(ObjectDoesNotExist):
            self.service.Absolve(self.request, self.grpc_context)
