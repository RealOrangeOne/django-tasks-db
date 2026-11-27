import time
from argparse import ArgumentParser, ArgumentTypeError
from pathlib import Path

from django.core.files import locks
from django.core.management.base import BaseCommand, CommandError


def valid_max_age(val: str) -> int:
    num = int(val)
    if num <= 0:
        raise ArgumentTypeError("Must be greater than zero")
    return num


class Command(BaseCommand):
    help = "Health check command to determine if a worker is currently running"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("lock_file", type=Path, help="The workers lock file path")
        parser.add_argument(
            "--max-age",
            type=valid_max_age,
            help="Maximum age of the lock file (in seconds)",
            default=None,
            nargs="?",
        )

    def handle(
        self, *, lock_file: Path, max_age: int | None, **options: object
    ) -> None:
        try:
            with lock_file.open("rb") as f:
                if locks.lock(f, locks.LOCK_EX | locks.LOCK_NB):
                    raise CommandError("No worker is running")
        except FileNotFoundError:
            raise CommandError("Lock file does not exist") from None

        if max_age is not None:
            if time.time() - max_age > lock_file.stat().st_mtime:
                raise CommandError("Lock file is too old.")

        self.stdout.write("Worker is running.")
