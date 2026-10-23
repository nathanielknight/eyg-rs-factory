from django.core.management.base import BaseCommand

from workflow.git import get_repo
from workflow.merging import merge, mergeable
from workflow.support import LockBusy, batch_lock


class Command(BaseCommand):
    help = (
        "Merge submissions that passed validation against current main and "
        "whose review, if required, was approved."
    )

    def handle(self, *args, **options):
        repo = get_repo()
        try:
            with batch_lock("merge"):
                for vr in mergeable():
                    if record := merge(repo, vr):
                        self.stdout.write(f"merged {vr}: {record.result_sha}")
        except LockBusy:
            self.stdout.write("another merge is running")
