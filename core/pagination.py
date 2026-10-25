import re
from abc import ABC, abstractmethod
from typing import Callable, Iterable, Iterator, Optional

import grpc
from django.conf import settings
from django.db import models

# Re-export for backward compatibility
from django.db.models import Model, Q, QuerySet
from google.protobuf.message import Message
from grpc_interceptor.exceptions import InvalidArgument

from core.models import MessageConvertible
from protos import pagination_pb2


class PaginationAdapter(ABC):
    def __init__(self, context: grpc.ServicerContext, query: QuerySet):
        self.context = context
        self.initial_query = query.order_by(*self.get_cursor_fields())
        self.query = self.initial_query
        self.forward = True

    @property
    def random_access(self) -> bool:
        return False

    @abstractmethod
    def get_cursor_fields(self) -> Iterable[str]:
        raise NotImplementedError

    def apply_header(self, header: pagination_pb2.Header):
        self.forward = header.forward

    def make_queryset_filters(self, page: pagination_pb2.Page) -> Q:
        filters = Q()
        equalities = {}

        if self.forward == page.cursor.is_next:
            comp_normal = "gt"
            comp_reverse = "lt"
        else:
            comp_normal = "lt"
            comp_reverse = "gt"

        for pair in page.cursor.data:
            comparator = comp_reverse if pair.key.startswith("-") else comp_normal
            stuff = {
                **equalities,
                pair.key.removeprefix("-") + "__" + comparator: pair.value,
            }
            filters |= Q(**stuff)
            equalities[pair.key] = pair.value

        return filters

    def make_cursor_data(self, item: Model) -> list[pagination_pb2.KeyValuePair]:
        return [
            pagination_pb2.KeyValuePair(key=field, value=str(getattr(item, field)))
            for field in self.get_cursor_fields()
        ]

    def make_message(self, item: Model, **overrides) -> Message:
        if isinstance(item, MessageConvertible):
            return item.to_message(context=self.context, **overrides)
        else:
            raise ValueError


class PaginatorMixin:
    def paginate(
        self,
        request_iterator: Iterator[pagination_pb2.Page],
        bundle_class: type,
        adapter: PaginationAdapter,
        message_overrides: dict = {},
        on_items: Optional[Callable[[list], None]] = None,
    ) -> Iterator[Message]:
        bundle_field = re.sub(r"(?<!^)(?=[A-Z])", "_", bundle_class.__name__).lower()
        header_received = False
        size = 0

        for request in request_iterator:
            position_type = request.WhichOneof("position")
            is_header = position_type == "header"

            if is_header:
                header_received = True
                size = request.header.size
                adapter.apply_header(request.header)

                if not (0 < size <= settings.PAGINATION_MAX_SIZE):
                    raise InvalidArgument("invalid_size")

                continue
            elif not header_received:
                raise InvalidArgument("missing_header")

            if position_type == "cursor":
                yield self._paginate_cursor(
                    request,
                    bundle_class,
                    bundle_field,
                    adapter,
                    message_overrides,
                    size,
                    on_items,
                )
            elif adapter.random_access:
                yield self._paginate_offset(
                    request,
                    bundle_class,
                    bundle_field,
                    adapter,
                    message_overrides,
                    size,
                    on_items,
                )
            else:
                raise InvalidArgument("random_access_unauthorized")

    def _paginate_cursor(
        self,
        page: pagination_pb2.Page,
        bundle_class: type,
        bundle_field: str,
        adapter: PaginationAdapter,
        message_overrides: dict,
        size: int,
        on_items: Optional[Callable[[list], None]],
    ) -> Message:
        previous_cursor = None
        next_cursor = None
        has_previous = False
        has_next = False

        items = adapter.query
        filters = adapter.make_queryset_filters(page)

        if not adapter.forward:
            items = items.reverse()

        items = items.filter(filters).fetch_mode(models.FETCH_PEERS)[: size + 1]

        if len(page.cursor.data) > 0:
            if page.cursor.is_next:
                has_previous = True
            else:
                has_next = True

        items = list(items)
        item_count = len(items)

        if item_count == 0:
            return bundle_class(**{bundle_field: []})

        if item_count > size:
            items.pop()
            item_count -= 1

            if page.cursor.is_next:
                has_next = True
            else:
                has_previous = True

        if has_previous:
            previous_cursor = pagination_pb2.Cursor(
                data=adapter.make_cursor_data(items[0]), is_next=False
            )

        if has_next:
            next_cursor = pagination_pb2.Cursor(
                data=adapter.make_cursor_data(items[-1]), is_next=True
            )

        if on_items:
            on_items(items)

        return bundle_class(
            **{
                bundle_field: [
                    adapter.make_message(item, **message_overrides) for item in items
                ],
                "previous": previous_cursor,
                "next": next_cursor,
            }
        )

    def _paginate_offset(
        self,
        page: pagination_pb2.Page,
        bundle_class: type,
        bundle_field: str,
        adapter: PaginationAdapter,
        message_overrides: dict,
        size: int,
        on_items: Optional[Callable[[list], None]],
    ) -> Message:
        items = adapter.query

        if not adapter.forward:
            items = items.reverse()

        count = items.count()
        items = items.fetch_mode(models.FETCH_PEERS)[page.offset : page.offset + size]

        if on_items:
            on_items(items)

        return bundle_class(
            **{
                bundle_field: [
                    adapter.make_message(item, **message_overrides) for item in items
                ],
                "count": count,
            }
        )
