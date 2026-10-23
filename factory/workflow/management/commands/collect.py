from django.core.management.base import BaseCommand

from runner.runner import get_runner
from workflow.dev import collect_all
from workflow.support import LockBusy, batch_lock


class Command(BaseCommand):
    help = (
        "Record how ended dev runs went, cancel those whose ticket was "
        "cancelled, and resume any whose worker died."
    )

    def handle(self, *args, **options):
        try:
            with batch_lock("dev"):
                collect_all(get_runner())
        except LockBusy:
            self.stdout.write("another dev worker is running")
