from django.core.management.base import BaseCommand

from runner.runner import get_runner


class Command(BaseCommand):
    help = "Delete run-* Incus instances that have no live run record."

    def handle(self, *args, **options):
        for name in get_runner().sweep():
            self.stdout.write(f"deleted {name}")
