from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from .models import CachedRequest
from .tasks import cleanup_cached_requests


class Task_cleanup_cached_requests(TestCase):
    def test(self):
        CachedRequest.objects.create(
            request_id="fresh",
            serialized_response="{}",
            serialized_response_message=b"x",
        )
        stale = CachedRequest.objects.create(
            request_id="stale",
            serialized_response="{}",
            serialized_response_message=b"x",
        )
        CachedRequest.objects.filter(id=stale.id).update(
            date_created=now() - timedelta(minutes=5)
        )
        cleanup_cached_requests.delay()
        self.assertFalse(CachedRequest.objects.filter(request_id="stale").exists())
        self.assertTrue(CachedRequest.objects.filter(request_id="fresh").exists())

    def test_too_soon(self):
        CachedRequest.objects.create(
            request_id="fresh",
            serialized_response="{}",
            serialized_response_message=b"x",
        )
        cleanup_cached_requests.delay()
        self.assertTrue(CachedRequest.objects.filter(request_id="fresh").exists())
