from argparse import ArgumentParser

from django.core.files import locks
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Health check command to determine if a worker is currently running"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("lock_file", help="The workers lock file path")

    def handle(self, *, lock_file: str, **options: object) -> None:
        try:
            with open(lock_file, "rb") as f:
                if locks.lock(f, locks.LOCK_EX | locks.LOCK_NB):
                    raise CommandError("No worker is running")
        except FileNotFoundError:
            raise CommandError("Lock file does not exist") from None

        self.stdout.write("Worker is running.")
