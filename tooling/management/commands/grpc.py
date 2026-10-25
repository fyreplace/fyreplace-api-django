import logging
import signal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import autoreload

from core.grpc import create_server

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        if settings.DEBUG:
            autoreload.run_with_reloader(self.run, *args, **kwargs)
        else:
            self.run()

    def run(self, *args, **kwargs):
        autoreload.raise_last_exception()

        if not settings.DEBUG:
            for sig in [signal.SIGHUP, signal.SIGINT, signal.SIGTERM]:
                signal.signal(sig, lambda signum, frame: self.stop_server())

        self.run_server()

    def run_server(self):
        self.server = create_server()

        try:
            logging.info("gRPC server starting")
            self.server.start()
            logging.info("gRPC server started")
            self.server.wait_for_termination()
        finally:
            logging.info("gRPC server stopping")
            self.stop_server()
            logging.info("gRPC server stopped")

    def stop_server(self, *args, **kwargs):
        self.server.stop(grace=10)
