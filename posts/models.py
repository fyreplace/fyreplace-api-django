from math import ceil
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Replace
from django.db.transaction import atomic
from django.utils.timezone import now
from grpc_interceptor.exceptions import InvalidArgument, PermissionDenied

from core.models import (
    ExistingManager,
    MessageConvertible,
    SoftDeleteModel,
    TimestampModel,
    UUIDModel,
)
from core.validators import FileSizeValidator
from protos import comment_pb2, post_pb2


def position_between(before: Optional[str], after: Optional[str]) -> str:
    for arg in (before, after):
        if not isinstance(arg, (str, type(None))):
            raise TypeError("Arguments must be strings or None")

    len_before = len(before) if before else 0
    len_after = len(after) if after else 0

    if not before and not after:
        return "z"
    elif before == after:
        raise RuntimeError("Arguments must be different")
    elif before and after and before > after:
        raise RuntimeError("Before and after are inverted")
    elif not after or len_before >= len_after:
        return before + "z"
    else:
        return after[:-1] + "a" + "z"


class ValidatableModel(UUIDModel, MessageConvertible):
    class Meta:
        abstract = True

    def validate(self):
        raise NotImplementedError


class PostQuerySet(models.QuerySet):
    def get_readable_by(self, author: AbstractUser, *args, **kwargs) -> "Post":
        return self.get(
            models.Q(author=author) | models.Q(date_published__isnull=False),
            *args,
            **kwargs,
        )

    def get_writable_by(self, author: AbstractUser, *args, **kwargs) -> "Post":
        post = self.get_readable_by(author, *args, **kwargs)

        if author.id != post.author_id or post.date_published:
            raise PermissionDenied(
                "post_published" if post.date_published else "invalid_post_author"
            )

        return post

    def get_published_readable_by(
        self, author: AbstractUser, *args, **kwargs
    ) -> "Post":
        post = self.get_readable_by(author, *args, **kwargs)

        if not post.date_published:
            raise PermissionDenied("post_not_published")

        return post


class ExistingPostManager(models.Manager.from_queryset(PostQuerySet)):
    def get_queryset(self) -> PostQuerySet:
        return PostQuerySet(self.model).filter(is_deleted=False)


class PublishedPostManager(ExistingPostManager):
    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(date_published__isnull=False)


class DraftPostManager(ExistingPostManager):
    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(date_published__isnull=True)


class ActivePostManager(ExistingPostManager):
    def get_queryset(self) -> models.QuerySet:
        deadline = now() - settings.FYREPLACE_POST_MAX_DURATION
        return super().get_queryset().filter(date_published__gte=deadline, life__gt=0)


class Post(TimestampModel, SoftDeleteModel, ValidatableModel):
    class Meta:
        ordering = ["date_published", "date_created", "id"]

    MAX_CHAPTERS = 10
    objects = models.Manager()
    existing_objects = ExistingPostManager()
    published_objects = PublishedPostManager()
    draft_objects = DraftPostManager()
    active_objects = ActivePostManager()
    default_message_class = post_pb2.Post

    author = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)ss"
    )
    voters = models.ManyToManyField(
        to=settings.AUTH_USER_MODEL,
        related_name="voted_%(class)ss",
        through="Vote",
    )
    subscribers = models.ManyToManyField(
        to=settings.AUTH_USER_MODEL,
        related_name="subscribed_%(class)ss",
        through="Subscription",
    )
    is_anonymous = models.BooleanField(default=False)
    date_published = models.DateTimeField(null=True)
    life = models.IntegerField(default=0, validators=[MinValueValidator(0)])

    @property
    def chapter_count(self) -> int:
        return self.chapters.count()

    @property
    def vote_count(self) -> int:
        return Vote.objects.filter(post_id=self.id).count()

    @property
    def comment_count(self) -> int:
        return Comment.existing_objects.filter(post_id=self.id).count()

    def __str__(self) -> str:
        return f"{self.author}: {self.chapters.count()} ({self.date_published or self.date_created})"

    def get_message_field_values(self, **overrides) -> dict:
        data = super().get_message_field_values(**overrides)
        chapters = data["chapters"].all()

        if overrides.get("is_preview", False):
            chapters = chapters[:1]

        data["chapters"] = [self.convert_value("chapters", c) for c in chapters]
        return data

    def convert_field(self, field: str) -> Any:
        return super().convert_field(
            field
            if field != "date_created" or not self.date_published
            else "date_published"
        )

    def delete(self, *args, **kwargs) -> Tuple[int, Dict[str, int]]:
        return (
            super().delete(*args, **kwargs)
            if self.date_published is None
            else self.soft_delete()
        )

    def perform_soft_delete(self):
        self.life = 0
        super().perform_soft_delete()

    @atomic
    def publish(self, anonymous: bool):
        if self.date_published is not None:
            raise PermissionDenied("already_published")

        self.validate()
        self.is_anonymous = anonymous
        self.date_published = now()
        self.life = 10
        self.save()
        self.subscribers.add(self.author)

    def validate(self):
        if self.chapters.count() == 0:
            raise InvalidArgument("post_empty")

        for chapter in self.chapters.all():
            chapter.validate()

    def chapter_position(self, position: int) -> str:
        positions = self.chapters.values_list("position", flat=True)
        chapter_count = len(positions)

        if position < 0 or position > chapter_count:
            raise InvalidArgument("invalid_position")

        before = None if position == 0 else positions[position - 1]
        after = (
            None
            if chapter_count == 0 or position >= chapter_count
            else positions[position]
        )
        return position_between(before, after)

    def get_chapter_at(self, position: int, for_update: bool = False) -> "Chapter":
        try:
            chapters = (
                self.chapters.select_for_update() if for_update else self.chapters.all()
            )
            return chapters[position]
        except IndexError:
            raise InvalidArgument("invalid_position")

    @atomic
    def normalize_chapters(self):
        self.chapters.select_for_update().update(
            position=Replace(
                Replace(models.F("position"), models.Value("z"), models.Value("y")),
                models.Value("a"),
                models.Value("b"),
            )
        )
        chapters = self.chapters.all()
        a_length = ceil(len(chapters) / 2)

        for i, chapter in enumerate(chapters):
            if i < a_length:
                chapter.position = "a" * (a_length - i) + "z"
            else:
                chapter.position = "z" * (i - a_length + 1)

            chapter.save()

    def overrides_for_user(self, user: Optional[AbstractUser]) -> bool:
        overrides = {}

        if self.is_anonymous and (not user or user.id != self.author_id):
            overrides["author"] = None

        if self.subscribers.filter(id=user.id).exists() if user else False:
            overrides["is_subscribed"] = True

        if subscription := self.subscriptions.filter(user=user).first():
            if comment := subscription.last_comment_seen:
                overrides["comments_read"] = comment.count(after=False) + 1

        return overrides


class Chapter(ValidatableModel):
    class Meta:
        unique_together = ["post", "position"]
        ordering = unique_together

    default_message_class = post_pb2.Chapter

    post = models.ForeignKey(
        to=Post, on_delete=models.CASCADE, related_name="%(class)ss"
    )
    position = models.CharField(max_length=128)
    text = models.CharField(
        max_length=500, blank=True, validators=[MaxLengthValidator(500)]
    )
    is_title = models.BooleanField(default=False)
    image = models.ImageField(
        width_field="width",
        height_field="height",
        upload_to="chapters",
        validators=[FileSizeValidator(max_bytes=512 * 1024)],
        null=True,
        blank=True,
    )
    width = models.IntegerField(
        null=True, blank=True, validators=[MinValueValidator(0)]
    )
    height = models.IntegerField(
        null=True, blank=True, validators=[MinValueValidator(0)]
    )

    def __str__(self) -> str:
        return f"{self.post}: {self.position}"

    def get_message_field_values(self, **overrides) -> dict:
        data = super().get_message_field_values(**overrides)

        if data["image"]:
            data["image"].width = self.width
            data["image"].height = self.height

        return data

    def validate(self):
        if not self.text and not self.image:
            raise InvalidArgument("chapter_empty")

    def clear(self, save: bool = True):
        self.text = ""
        self.is_title = False
        self.image.delete(save=False)

        if save:
            self.save()


class Stack(UUIDModel):
    class Meta:
        ordering = ["user"]

    MAX_SIZE = 10

    user = models.OneToOneField(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)s"
    )
    posts = models.ManyToManyField(to=Post, related_name="+", through="Visibility")
    date_last_filled = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.user)

    @atomic
    def fill(self):
        posts_count = self.posts.count()

        if posts_count >= self.MAX_SIZE:
            return

        post_ids = (
            Post.active_objects.select_for_update()
            .exclude(author=self.user)
            .exclude(author__in=self.user.blocked_users.values("id"))
            .exclude(author__in=self.user.blocking_users.values("id"))
            .exclude(voters=self.user)
            .order_by("date_published")
            .values_list("id", flat=True)[: self.MAX_SIZE]
        )
        current_post_ids = list(self.posts.values_list("id", flat=True))
        self.posts.set(post_ids)
        self.posts.exclude(id__in=current_post_ids).update(life=models.F("life") - 1)
        self.save()

    @atomic
    def drain(self):
        self.posts.update(life=models.F("life") + 1)
        self.posts.clear()


class Vote(UUIDModel):
    class Meta:
        unique_together = ["user", "post"]
        ordering = unique_together

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    post = models.ForeignKey(to=Post, on_delete=models.CASCADE, related_name="+")
    spread = models.BooleanField()

    def __str__(self) -> str:
        return f"{self.user}, {self.post}: {self.spread}"


class Visibility(UUIDModel):
    class Meta:
        unique_together = ["stack", "post"]
        ordering = unique_together

    stack = models.ForeignKey(to=Stack, on_delete=models.CASCADE, related_name="+")
    post = models.ForeignKey(to=Post, on_delete=models.CASCADE, related_name="+")

    def __str__(self) -> str:
        return f"{self.stack}, {self.post}"


class Comment(UUIDModel, TimestampModel, SoftDeleteModel):
    class Meta:
        ordering = ["date_created", "id"]

    objects = models.Manager()
    existing_objects = ExistingManager()
    default_message_class = comment_pb2.Comment

    post = models.ForeignKey(
        to=Post, on_delete=models.CASCADE, related_name="%(class)ss"
    )
    author = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    text = models.CharField(max_length=1500, validators=[MaxLengthValidator(1500)])

    @property
    def position(self) -> int:
        return self.count(after=False)

    def count(self, after: bool, count_deleted: bool = True) -> int:
        date_created_mod = "gte" if after else "lte"
        id_mod = "lte" if after else "gte"
        comments = Comment.objects.filter(
            post_id=self.post_id,
            **{"date_created__" + date_created_mod: self.date_created},
        ).exclude(
            date_created=self.date_created,
            **{"id__" + id_mod: self.id},
        )

        if not count_deleted:
            comments = comments.exclude(is_deleted=True)

        return comments.count()

    def __str__(self) -> str:
        return f"{self.author}, {self.post} ({self.date_created})"

    def get_message_fields(self, **overrides) -> list[str]:
        fields = super().get_message_fields(**overrides)

        if self.is_deleted:
            fields.remove("text")

        if not overrides.get("is_preview"):
            fields.remove("position")

        return fields

    def delete(self, *args, **kwargs) -> Tuple[int, Dict[str, int]]:
        return self.soft_delete()

    def perform_soft_delete(self):
        self.text = ""
        super().perform_soft_delete()


class Subscription(UUIDModel):
    class Meta:
        unique_together = ["user", "post"]
        ordering = unique_together

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)ss"
    )
    post = models.ForeignKey(
        to=Post, on_delete=models.CASCADE, related_name="%(class)ss"
    )
    last_comment_seen = models.ForeignKey(
        to=Comment, on_delete=models.SET_NULL, related_name="+", null=True
    )
    date_last_seen = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.user}, {self.post}"
