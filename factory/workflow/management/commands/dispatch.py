from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from runner.runner import get_runner
from workflow.dev import collect_all, dispatch_next
from workflow.git import get_repo
from workflow.support import LockBusy, batch_lock


class Command(BaseCommand):
    help = (
        "Finish off live dev runs, then dispatch the oldest open ticket to an "
        "agent VM and run it to the end. Blocks for the length of the run."
    )

    def handle(self, *args, **options):
        if not settings.WORKFLOW_CLAUDE_TOKEN:
            raise CommandError("CLAUDE_CODE_OAUTH_TOKEN is not set")
        runner = get_runner()
        try:
            with batch_lock("dev"):
                collect_all(runner)
                dev_run = dispatch_next(runner, get_repo())
        except LockBusy:
            self.stdout.write("another dev worker is running")
            return
        if dev_run is None:
            self.stdout.write("nothing to dispatch")
        else:
            self.stdout.write(f"{dev_run}: {dev_run.outcome or dev_run.detail}")
