from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from runner.runner import get_runner
from workflow.git import get_repo
from workflow.support import LockBusy, batch_lock
from workflow.validation import to_validate, validate


class Command(BaseCommand):
    help = (
        "Validate pending submissions (and retry errored or stale ones): gate "
        "checks, then CI in an ephemeral VM. Results go to the ticket."
    )

    def handle(self, *args, **options):
        # Validating against an empty suite would wave everything through.
        if not settings.WORKFLOW_HOLDOUT_SUITES:
            raise CommandError("WORKFLOW_HOLDOUT_SUITES is not set")
        runner, repo = get_runner(), get_repo()
        try:
            with batch_lock("validate"):
                for submission in to_validate(repo):
                    vr = validate(runner, repo, submission)
                    self.stdout.write(f"{vr}: {vr.outcome or vr.detail}")
        except LockBusy:
            self.stdout.write("another validate is running")
