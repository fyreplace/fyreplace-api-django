from django.contrib.auth import get_user_model
from grpc_interceptor.exceptions import InvalidArgument

from core.pagination import PaginatorMixin
from protos import pagination_pb2, user_pb2
from users.tests import AuthenticatedTestCase, UserContext, make_email

from .pagination import PaginationAdapter


class UsersAdapter(PaginationAdapter):
    def get_cursor_fields(self):
        return ["username", "id"]


class MakeQuerySetFilters(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = UsersAdapter(self.grpc_context, get_user_model().objects.all())

        for username in ("aaa", "aab", "aba"):
            get_user_model().objects.create_user(
                username=username, email=make_email(username)
            )

        self.users = {u.username: u for u in get_user_model().objects.all()}

    def make_filters(
        self, username: str, cursor: pagination_pb2.Cursor, forward: bool = True
    ):
        self.adapter.forward = forward
        return self.adapter.make_queryset_filters(pagination_pb2.Page(cursor=cursor))

    def make_cursor(self, username: str, is_next: bool) -> pagination_pb2.Cursor:
        user = self.users[username]
        return pagination_pb2.Cursor(
            data=[
                pagination_pb2.KeyValuePair(key="username", value=username),
                pagination_pb2.KeyValuePair(key="id", value=str(user.id)),
            ],
            is_next=is_next,
        )

    def test_next(self):
        filters = self.make_filters(
            "aab", self.make_cursor("aab", is_next=True), forward=True
        )
        usernames = [
            u.username
            for u in get_user_model().objects.filter(filters).order_by("username")
        ]
        self.assertEqual([u for u in usernames if u.startswith("a")], ["aba"])

    def test_previous(self):
        filters = self.make_filters(
            "aab", self.make_cursor("aab", is_next=False), forward=True
        )
        usernames = [
            u.username
            for u in get_user_model().objects.filter(filters).order_by("username")
        ]
        self.assertEqual([u for u in usernames if u.startswith("a")], ["aaa"])

    def test_reversed_field(self):
        cursor = pagination_pb2.Cursor(
            data=[pagination_pb2.KeyValuePair(key="-username", value="aab")],
            is_next=True,
        )
        filters = self.make_filters("aab", cursor, forward=True)
        usernames = [
            u.username
            for u in get_user_model().objects.filter(filters).order_by("username")
        ]
        self.assertEqual([u for u in usernames if u.startswith("a")], ["aaa"])

    def test_empty_cursor(self):
        filters = self.adapter.make_queryset_filters(
            pagination_pb2.Page(cursor=pagination_pb2.Cursor(is_next=True))
        )
        self.assertEqual(
            get_user_model().objects.filter(filters).count(),
            get_user_model().objects.count(),
        )


class MakeCursorData(AuthenticatedTestCase):
    def test(self):
        adapter = UsersAdapter(self.grpc_context, get_user_model().objects.all())
        pairs = adapter.make_cursor_data(self.main_user)
        self.assertEqual(
            [(pair.key, pair.value) for pair in pairs],
            [("username", self.main_user.username), ("id", str(self.main_user.id))],
        )


class Paginate(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.mixin = PaginatorMixin()
        self.adapter = UsersAdapter(self.grpc_context, get_user_model().objects.all())

    def paginate(self, requests):
        return list(
            self.mixin.paginate(
                iter(requests),
                bundle_class=user_pb2.Profiles,
                adapter=self.adapter,
                message_overrides={"message_class": user_pb2.Profile},
            )
        )

    def test_missing_header(self):
        requests = [pagination_pb2.Page(cursor=pagination_pb2.Cursor(is_next=True))]

        with self.assertRaises(InvalidArgument):
            self.paginate(requests)

    def test_invalid_size(self):
        for size in [0, 51]:
            requests = [
                pagination_pb2.Page(
                    header=pagination_pb2.Header(forward=True, size=size)
                )
            ]

            with self.assertRaises(InvalidArgument):
                self.paginate(requests)

    def test_random_access_unauthorized(self):
        requests = [
            pagination_pb2.Page(header=pagination_pb2.Header(forward=True, size=5)),
            pagination_pb2.Page(offset=0),
        ]

        with self.assertRaises(InvalidArgument):
            self.paginate(requests)

    def test_empty(self):
        self.adapter.query = self.adapter.query.filter(username="nobody")
        requests = [
            pagination_pb2.Page(header=pagination_pb2.Header(forward=True, size=5)),
            pagination_pb2.Page(cursor=pagination_pb2.Cursor(is_next=True)),
        ]
        [profiles] = self.paginate(requests)
        self.assertEqual(len(profiles.profiles), 0)

    def test(self):
        requests = [
            pagination_pb2.Page(header=pagination_pb2.Header(forward=True, size=5)),
            pagination_pb2.Page(cursor=pagination_pb2.Cursor(is_next=True)),
        ]
        [profiles] = self.paginate(requests)
        usernames = {p.username for p in profiles.profiles}
        self.assertIn(self.main_user.username, usernames)
        self.assertIn(self.other_user.username, usernames)
