from django.db.models import Model
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.signals import post_soft_delete

from .models import Flag, Notification, remove_notifications_for


@receiver(post_soft_delete)
def on_post_soft_delete(instance: Model, **kwargs):
    remove_notifications_for(instance)


@receiver(post_save, sender=Notification)
def on_notification_post_save(instance: Notification, **kwargs):
    if instance.count == 0:
        instance.delete()


@receiver(post_save, sender=Flag)
def on_flag_post_save(instance: Flag, **kwargs):
    notification, created = Notification.objects.get_or_create(
        subscription=None,
        target_type=instance.target_type,
        target_id=instance.target_id,
    )

    if not created:
        notification.save()
