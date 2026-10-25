from typing import Iterator

import grpc
from django.db.models import F, OuterRef
from django.db.transaction import atomic
from google.protobuf import empty_pb2

from core.pagination import PaginatorMixin
from posts.models import Comment, Subscription
from protos import notification_pb2, notification_pb2_grpc, pagination_pb2

from .models import Notification, RemoteMessaging, count_notifications_for
from .pagination import NotificationPaginationAdapter
from .tasks import send_remote_notifications_clear


class NotificationService(
    PaginatorMixin, notification_pb2_grpc.NotificationServiceServicer
):
    def Count(
        self, request: empty_pb2.Empty, context: grpc.ServicerContext
    ) -> notification_pb2.NotificationCount:
        return notification_pb2.NotificationCount(
            count=count_notifications_for(context.caller)
        )

    def List(
        self,
        request_iterator: Iterator[pagination_pb2.Page],
        context: grpc.ServicerContext,
    ) -> Iterator[notification_pb2.Notifications]:
        notifications = Notification.objects.filter_readable_by(context.caller)
        return self.paginate(
            request_iterator,
            bundle_class=notification_pb2.Notifications,
            adapter=NotificationPaginationAdapter(context, notifications),
            message_overrides={"is_preview": True},
        )

    def Clear(
        self, request: empty_pb2.Empty, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        Subscription.objects.filter(user=context.caller).annotate(
            last_comment_id=(
                Comment.objects.filter(post_id=OuterRef("post_id"))
                .order_by("-date_created", "-id")
                .values("id")[:1]
            )
        ).update(last_comment_seen_id=F("last_comment_id"))

        Notification.objects.filter(
            subscription__in=Subscription.objects.filter(user=context.caller)
        ).delete()

        send_remote_notifications_clear.delay(user_id=str(context.caller.id))
        return empty_pb2.Empty()

    @atomic
    def RegisterToken(
        self, request: notification_pb2.MessagingToken, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        remote_messaging = RemoteMessaging(
            connection=context.caller_connection,
            service=request.service,
            token=request.token,
        )
        remote_messaging.full_clean(validate_unique=False)

        RemoteMessaging.objects.filter(connection=context.caller_connection).exclude(
            token=request.token
        ).delete()
        RemoteMessaging.objects.update_or_create(
            connection=context.caller_connection,
            defaults={
                "service": request.service,
                "token": request.token,
            },
        )
        return empty_pb2.Empty()
