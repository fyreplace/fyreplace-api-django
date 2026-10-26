from datetime import timedelta
from typing import List, Optional

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import OuterRef
from firebase_admin import exceptions, messaging

from posts.models import Comment
from users.models import Block, Connection

from ..models import MessagingService, RemoteMessaging
from . import b64encode, cut_text


@shared_task(autoretry_for=[ObjectDoesNotExist], retry_backoff=True)
def send_remote_notifications_comment_change(comment_id: str):
    comment = Comment.objects.get(id=comment_id)
    payload = make_payload(
        comment, "comment:" + ("deletion" if comment.is_deleted else "creation")
    )
    connections = Connection.objects.filter(
        user_id__in=comment.post.subscribers.exclude(id=comment.author_id).values("id")
    ).exclude(
        user_id__in=Block.objects.filter(
            issuer_id=OuterRef("user_id"), target_id=comment.author_id
        ).values("issuer_id")
    )
    own_remote_messagings = RemoteMessaging.objects.filter(
        service=MessagingService.FCM,
        connection_id__in=connections.filter(user_id=comment.post.author_id),
    )
    other_remote_messagings = RemoteMessaging.objects.filter(
        service=MessagingService.FCM,
        connection_id__in=connections.exclude(user_id=comment.post.author_id),
    )

    for remote_messagings, channel_id in zip(
        (own_remote_messagings, other_remote_messagings),
        ("comments_own_posts", "comments_other_posts"),
    ):
        for cursor in range(0, remote_messagings.count(), 500):
            chunk = remote_messagings[cursor : cursor + 500]
            batch_response = send_multicast_message(
                make_multicast_message(
                    list(chunk.values_list("token", flat=True)),
                    channel_id,
                    payload,
                    comment,
                )
            )

            if not batch_response:
                break

            for response, remote_messaging in zip(batch_response.responses, chunk):
                if not response.exception:
                    continue
                elif response.exception.code == exceptions.NOT_FOUND:
                    remote_messaging.delete()
                else:
                    raise response.exception


@shared_task(autoretry_for=[ObjectDoesNotExist], retry_backoff=True)
def send_remote_notifications_comment_acknowledgement(comment_id: str, user_id: str):
    comment = Comment.objects.get(id=comment_id)

    if user_id == comment.author_id:
        return

    remote_messagings = RemoteMessaging.objects.filter(
        service=MessagingService.FCM,
        connection__in=Connection.objects.filter(user_id=user_id),
    )

    send_multicast_message(
        make_multicast_message(
            list(remote_messagings.values_list("token", flat=True)),
            "",
            make_payload(comment, "comment:acknowledgement"),
            comment,
        )
    )


@shared_task
def send_remote_notifications_clear(user_id: str):
    remote_messagings = RemoteMessaging.objects.filter(
        service=MessagingService.FCM,
        connection__in=Connection.objects.filter(user_id=user_id),
    )

    send_multicast_message(
        make_multicast_message(
            list(remote_messagings.values_list("token", flat=True)),
            "",
            make_payload(None, "notifications:clear"),
            None,
        )
    )


def send_multicast_message(
    message: messaging.MulticastMessage,
) -> Optional[messaging.BatchResponse]:
    if settings.FIREBASE_APP and message.tokens:
        return messaging.send_each_for_multicast(message)
    else:
        return None


def make_payload(comment: Optional[Comment], command: str) -> dict:
    payload = {"_command": command}

    if comment:
        payload["comment"] = b64encode(
            comment.to_message().SerializeToString(deterministic=True)
        )
        payload["postId"] = b64encode(comment.post_id)

    return payload


def make_multicast_message(
    tokens: List[str], channel_id: str, payload: dict, comment: Optional[Comment]
) -> messaging.MulticastMessage:
    is_silent = payload["_command"] != "comment:creation"
    return messaging.MulticastMessage(
        tokens=tokens,
        android=messaging.AndroidConfig(
            ttl=timedelta(weeks=1),
            notification=(
                None
                if is_silent
                else messaging.AndroidNotification(
                    title=comment.author.username,
                    body=cut_text(comment.text),
                    tag=make_notification_tag(comment),
                    channel_id=channel_id,
                    event_timestamp=comment.date_created,
                )
            ),
            data={"_fcm.channel": channel_id, **payload} if is_silent else payload,
        ),
    )


def make_notification_tag(comment: Comment) -> str:
    return f"{b64encode(comment.post_id)}:{b64encode(comment.id)}"
