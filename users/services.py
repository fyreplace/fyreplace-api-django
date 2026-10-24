import re
import unicodedata
import uuid
from datetime import timedelta
from os import path
from typing import Iterator

import grpc
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.transaction import atomic
from email_validator import EmailNotValidError, validate_email
from google.protobuf import empty_pb2
from grpc_interceptor.exceptions import (
    AlreadyExists,
    Cancelled,
    InvalidArgument,
    PermissionDenied,
)

from core import jwt
from core.authentication import no_auth
from core.grpc import get_info_from_token, serialize_message
from core.pagination import PaginatorMixin
from core.services import ImageUploadMixin
from notifications.models import Flag, remove_notifications_for
from protos import id_pb2, image_pb2, pagination_pb2, user_pb2, user_pb2_grpc

from .models import Connection, User
from .pagination import UsersPaginationAdapter
from .tasks import (
    fetch_default_user_avatar,
    send_account_activation_email,
    send_account_connection_email,
    send_user_email_update_email,
    use_connection,
)
from .validators import validate_unicode_username


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore")
    return re.sub(r"[^\w]", "", value.decode("ascii")).strip().upper()


def check_email(email: str):
    try:
        validate_email(email)
    except EmailNotValidError:
        raise InvalidArgument("invalid_email")


def check_user(user: User):
    if not user.is_alive_and_kicking:
        if user.is_pending:
            message_end = "pending"
        elif user.is_deleted:
            message_end = "deleted"
        else:
            message_end = "banned"

            if not user.date_ban_end:
                message_end += "_permanently"

        raise PermissionDenied("caller_" + message_end)


class AccountService(user_pb2_grpc.AccountServiceServicer):
    def __init__(self):
        super().__init__()

        with open(path.join(__package__, "reserved-usernames.txt"), "r") as reserved:
            self.reserved_usernames = [normalize(name) for name in reserved]

    @no_auth
    def Create(
        self, request: user_pb2.UserCreation, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        data = serialize_message(request)
        User = get_user_model()

        if normalize(request.username) in self.reserved_usernames:
            raise PermissionDenied("username_reserved")
        elif User.objects.filter(email=request.email).exists():
            raise AlreadyExists("email_taken")
        elif User.objects.filter(username=request.username).exists():
            raise AlreadyExists("username_taken")

        try:
            check_email(request.email)
            validate_unicode_username(request.username)
        except ValidationError:
            raise InvalidArgument("invalid_username")

        with atomic():
            user = User.objects.create_user(**data, is_active=False)
            user.full_clean()

        user_id = str(user.id)
        send_account_activation_email.delay(user_id=user_id)
        fetch_default_user_avatar.delay(user_id=user_id)
        return empty_pb2.Empty()

    def Delete(
        self, request: empty_pb2.Empty, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        context.caller.delete()
        return empty_pb2.Empty()

    @no_auth
    def SendActivationEmail(
        self, request: user_pb2.Email, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        if user := get_user_model().objects.filter(email=request.email).first():
            if user.is_pending:
                send_account_activation_email.delay(user_id=str(user.id))

        return empty_pb2.Empty()

    @no_auth
    @atomic
    def ConfirmActivation(
        self, request: user_pb2.ConnectionToken, context: grpc.ServicerContext
    ) -> user_pb2.Token:
        user, _ = get_info_from_token(request.token, for_update=True)

        if not user.is_pending:
            raise PermissionDenied("user_not_pending")

        user.is_active = True
        user.save()
        connection = Connection(
            user=user,
            hardware=request.client.hardware,
            software=request.client.software,
        )
        connection.full_clean()
        connection.save()
        return user_pb2.Token(token=connection.get_token())

    def ListConnections(
        self, request: empty_pb2.Empty, context: grpc.ServicerContext
    ) -> user_pb2.Connections:
        connections = Connection.objects.filter(user=context.caller)
        return user_pb2.Connections(
            connections=[c.to_message(context=context) for c in connections]
        )

    @no_auth
    def SendConnectionEmail(
        self, request: user_pb2.Email, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        with atomic():
            try:
                user = (
                    get_user_model()
                    .objects.select_for_update()
                    .get(email=request.email)
                )
            except ObjectDoesNotExist:
                check_email(request.email)
                raise

            check_user(user)

            if user.has_usable_password():
                raise Cancelled("caller_has_password")

            user.connection_token = uuid.uuid4()
            user.save()

        check_email(request.email)
        send_account_connection_email.delay(user_id=str(user.id))
        return empty_pb2.Empty()

    @no_auth
    def ConfirmConnection(
        self, request: user_pb2.ConnectionToken, context: grpc.ServicerContext
    ) -> user_pb2.Token:
        user, _ = get_info_from_token(request.token)
        check_user(user)
        connection_token = jwt.decode(request.token).get("connection_token")

        if connection_token != str(user.connection_token):
            raise PermissionDenied("invalid_connection_token")

        with atomic():
            user = get_user_model().objects.select_for_update().get(id=user.id)
            user.connection_token = None
            user.save()
            connection = Connection(
                user=user,
                hardware=request.client.hardware,
                software=request.client.software,
            )
            connection.full_clean()
            connection.save()

        return user_pb2.Token(token=connection.get_token())

    @no_auth
    @atomic
    def Connect(
        self, request: user_pb2.ConnectionCredentials, context: grpc.ServicerContext
    ) -> user_pb2.Token:
        user = get_user_model().objects.get(email=request.email)
        check_user(user)

        if not user.check_password(request.password):
            raise InvalidArgument("invalid_password")

        connection = Connection(
            user=user,
            hardware=request.client.hardware,
            software=request.client.software,
        )
        connection.full_clean()
        connection.save()

        return user_pb2.Token(token=connection.get_token())

    def Disconnect(
        self, request: id_pb2.Id, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        Connection.objects.get(
            id__bytes=request.id or context.caller_connection.id, user=context.caller
        ).delete()
        return empty_pb2.Empty()

    def DisconnectAll(
        self, request: empty_pb2.Empty, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        Connection.objects.filter(user=context.caller).delete()
        return empty_pb2.Empty()


class UserService(PaginatorMixin, ImageUploadMixin, user_pb2_grpc.UserServiceServicer):
    @no_auth
    def Retrieve(
        self, request: id_pb2.Id, context: grpc.ServicerContext
    ) -> user_pb2.User:
        return (
            get_user_model()
            .existing_objects.get(id__bytes=request.id)
            .to_message(context=context)
        )

    def RetrieveMe(
        self, request: empty_pb2.Empty, context: grpc.ServicerContext
    ) -> user_pb2.User:
        use_connection.delay(connection_id=str(context.caller_connection.id))
        return context.caller.to_message(context=context)

    @atomic
    def UpdateBio(
        self, request: user_pb2.Bio, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        user = get_user_model().objects.select_for_update().get(id=context.caller.id)
        user.bio = request.bio
        user.full_clean()
        user.save()
        return empty_pb2.Empty()

    @atomic
    def UpdateAvatar(
        self,
        request_iterator: Iterator[image_pb2.ImageChunk],
        context: grpc.ServicerContext,
    ) -> image_pb2.Image:
        image = self.get_image(request_iterator)
        user = get_user_model().objects.select_for_update().get(id=context.caller.id)
        self.set_image(user, "avatar", image)
        return user.to_message(message_class=user_pb2.Profile).avatar

    def SendEmailUpdateEmail(
        self, request: user_pb2.Email, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        check_email(request.email)

        if get_user_model().objects.filter(email=request.email).exists():
            raise AlreadyExists("email_taken")

        send_user_email_update_email.delay(
            user_id=str(context.caller.id), email=request.email
        )
        return empty_pb2.Empty()

    @atomic
    def ConfirmEmailUpdate(
        self, request: user_pb2.Token, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        user = get_user_model().objects.select_for_update().get(id=context.caller.id)
        request_user, _ = get_info_from_token(request.token)

        if request_user != user:
            raise PermissionDenied("invalid_user")

        user.email = jwt.decode(request.token).get("email")
        user.full_clean()
        user.save()
        return empty_pb2.Empty()

    def ListBlocked(
        self,
        request_iterator: Iterator[pagination_pb2.Page],
        context: grpc.ServicerContext,
    ) -> user_pb2.Profiles:
        users = context.caller.blocked_users.all()
        return self.paginate(
            request_iterator,
            bundle_class=user_pb2.Profiles,
            adapter=UsersPaginationAdapter(context, users),
            message_overrides={"message_class": user_pb2.Profile},
        )

    def UpdateBlock(
        self, request: user_pb2.Block, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        user = get_user_model().existing_objects.get(id__bytes=request.id)

        if user == context.caller:
            raise PermissionDenied("user_is_caller")
        elif request.blocked:
            context.caller.blocked_users.add(user)
        else:
            context.caller.blocked_users.remove(user)

        return empty_pb2.Empty()

    def Report(
        self, request: id_pb2.Id, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        user = get_user_model().existing_objects.get(id__bytes=request.id)

        if user == context.caller:
            raise PermissionDenied("user_is_caller")
        elif not user.is_active:
            raise PermissionDenied("user_not_active")
        elif user.is_banned:
            raise PermissionDenied("user_banned")
        elif user.rank > context.caller.rank:
            raise PermissionDenied("caller_rank_insufficient")

        Flag.objects.get_or_create(
            issuer=context.caller,
            target_type=ContentType.objects.get_for_model(get_user_model()),
            target_id=str(user.id),
        )
        return empty_pb2.Empty()

    def Absolve(
        self, request: id_pb2.Id, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        user = get_user_model().existing_objects.get(id__bytes=request.id)

        if user == context.caller:
            raise PermissionDenied("user_is_caller")
        elif user.rank >= context.caller.rank:
            raise PermissionDenied("caller_rank_insufficient")

        remove_notifications_for(user)
        return empty_pb2.Empty()

    @atomic
    def Ban(
        self, request: user_pb2.BanSentence, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        user = (
            get_user_model()
            .existing_objects.select_for_update()
            .get(id__bytes=request.id)
        )

        if user == context.caller:
            raise PermissionDenied("user_is_caller")
        elif user.rank >= context.caller.rank:
            raise PermissionDenied("caller_rank_insufficient")

        user.ban(timedelta(days=request.days) if request.days else None)
        return empty_pb2.Empty()

    @atomic
    def Promote(
        self, request: user_pb2.Promotion, context: grpc.ServicerContext
    ) -> empty_pb2.Empty:
        if not context.caller.is_superuser:
            raise PermissionDenied("caller_rank_insufficient")

        user = (
            get_user_model()
            .existing_objects.select_for_update()
            .get(id__bytes=request.id)
        )

        if request.rank == user_pb2.RANK_UNSPECIFIED:
            raise InvalidArgument("RANK_unspecified")
        elif request.rank == user_pb2.RANK_SUPERUSER and not user.is_staff:
            raise PermissionDenied("unsupported_promotion")

        user.is_staff = request.rank >= user_pb2.RANK_STAFF
        user.is_superuser = request.rank >= user_pb2.RANK_SUPERUSER
        user.save()
        return empty_pb2.Empty()
