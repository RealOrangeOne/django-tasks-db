import logging
import math
import os
import random
import signal
import sys
import time
from argparse import ArgumentParser, ArgumentTypeError, BooleanOptionalAction
from types import FrameType

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.utils import OperationalError
from django.utils.autoreload import DJANGO_AUTORELOAD_ENV, run_with_reloader
from django.utils.crypto import get_random_string

from django_tasks_db.backend import DatabaseBackend
from django_tasks_db.compat import (
    DEFAULT_TASK_BACKEND_ALIAS,
    DEFAULT_TASK_QUEUE_NAME,
    TASKS_LOGGER,
    InvalidTaskBackend,
    TaskContext,
    task_backends,
    task_finished,
    task_started,
)
from django_tasks_db.models import DBTaskResult
from django_tasks_db.utils import exclusive_transaction, is_locked_database_exception

logger = logging.getLogger("django_tasks_db")


class Worker:
    def __init__(
        self,
        *,
        queue_names: list[str],
        interval: float,
        batch: bool,
        backend_name: str,
        startup_delay: bool,
        max_tasks: int | None,
        worker_id: str,
        excluded_queue_names: list[str],
    ):
        self.queue_names = queue_names
        self.process_all_queues = "*" in queue_names
        self.excluded_queue_names = excluded_queue_names
        self.interval = interval
        self.batch = batch
        self.backend_name = backend_name
        self.startup_delay = startup_delay
        self.max_tasks = max_tasks

        self.running = True
        self.running_task = False
        self._run_tasks = 0

        self.worker_id = worker_id

    def shutdown(self, signum: int, frame: FrameType | None) -> None:
        if not self.running:
            logger.warning(
                "Received %s - terminating current task.", signal.strsignal(signum)
            )
            self.reset_signals()
            sys.exit(1)

        logger.warning(
            "Received %s - shutting down gracefully... (press Ctrl+C again to force)",
            signal.strsignal(signum),
        )
        self.running = False

        if not self.running_task:
            # If we're not currently running a task, exit immediately.
            # This is useful if we're currently in a `sleep`.
            sys.exit(0)

    def configure_signals(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        if hasattr(signal, "SIGQUIT"):
            signal.signal(signal.SIGQUIT, self.shutdown)

    def reset_signals(self) -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if hasattr(signal, "SIGQUIT"):
            signal.signal(signal.SIGQUIT, signal.SIG_DFL)

    def run(self) -> None:
        logger.info(
            "Starting worker worker_id=%s queues=%s",
            self.worker_id,
            ",".join(self.queue_names),
        )

        if self.startup_delay and self.interval:
            # Add a random small delay before starting to avoid a thundering herd
            time.sleep(random.random())  # noqa: S311

        while self.running:
            # Check for dropped/expired connections right after waking up
            close_old_connections()

            tasks = DBTaskResult.objects.ready().filter(backend_name=self.backend_name)
            if not self.process_all_queues:
                tasks = tasks.filter(queue_name__in=self.queue_names)
            if self.excluded_queue_names:
                tasks = tasks.exclude(queue_name__in=self.excluded_queue_names)

            with exclusive_transaction(tasks.db):
                try:
                    task_result = tasks.get_locked()
                    retrieved_task_result = True

                    if task_result is not None:
                        # "claim" the task, so it isn't run by another worker process
                        task_result.claim(self.worker_id)
                except OperationalError as e:
                    retrieved_task_result = False

                    # Ignore locked databases and keep trying.
                    # It should unlock eventually.
                    if is_locked_database_exception(e):
                        task_result = None
                    else:
                        raise

            if task_result is not None:
                self.run_task(task_result)

            if self.batch and retrieved_task_result and task_result is None:
                # If we're running in "batch" mode, terminate the loop (and thus the worker)
                logger.info(
                    "No more tasks to run for worker_id=%s - exiting gracefully.",
                    self.worker_id,
                )
                return None

            if self.max_tasks is not None and self._run_tasks >= self.max_tasks:
                logger.info(
                    "Run maximum tasks (%d) on worker=%s - exiting gracefully.",
                    self._run_tasks,
                    self.worker_id,
                )
                return None

            # Emulate Django's request behaviour and check for expired
            # database connections periodically.
            close_old_connections()

            # If ctrl-c has just interrupted a task, self.running was cleared,
            # and we should not sleep, but rather exit immediately.
            if self.running and not task_result:
                # Wait before checking for another task
                time.sleep(self.interval)

    def run_task(self, db_task_result: DBTaskResult) -> None:
        """
        Run the given task, marking it as successful or failed.
        """
        try:
            self.running_task = True
            task = db_task_result.task
            task_result = db_task_result.task_result

            backend = task.get_backend()
            backend_type = type(backend)

            task_started.send(sender=backend_type, task_result=task_result)
            if task.takes_context:
                return_value = task.call(
                    TaskContext(task_result=task_result),
                    *task_result.args,
                    **task_result.kwargs,
                )
            else:
                return_value = task.call(*task_result.args, **task_result.kwargs)

            # Setting the return and success value inside the error handling,
            # So errors setting it (eg JSON encode) can still be recorded
            db_task_result.set_successful(return_value)
        except BaseException as e:
            db_task_result.set_failed(e)

            try:
                sender = type(db_task_result.task.get_backend())
                task_result = db_task_result.task_result
            except (ImportError, SuspiciousOperation):
                logger.exception("Task id=%s failed unexpectedly", db_task_result.id)
            else:
                task_finished.send(
                    sender=sender,
                    task_result=task_result,
                )
        else:
            # Sent outside of the error handling, so an exception from a receiver
            # doesn't mark a task which has already run as failed.
            task_finished.send(
                sender=backend_type, task_result=db_task_result.task_result
            )
        finally:
            self.running_task = False
            self._run_tasks += 1


def valid_backend_name(val: str) -> str:
    try:
        backend = task_backends[val]
    except InvalidTaskBackend as e:
        raise ArgumentTypeError(e.args[0]) from e
    if not isinstance(backend, DatabaseBackend):
        raise ArgumentTypeError(f"Backend '{val}' is not a database backend")
    return val


def valid_interval(val: str) -> float:
    num = float(val)
    if not math.isfinite(num):
        raise ArgumentTypeError("Must be a finite floating point value")
    if num < 0:
        raise ArgumentTypeError("Must be zero or greater")
    return num


def valid_max_tasks(val: str) -> int:
    num = int(val)
    if num < 1:
        raise ArgumentTypeError("Must be one or greater")
    return num


class Command(BaseCommand):
    help = "Run a worker to process database-backed tasks."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--queue",
            "-q",
            action="append",
            dest="queue_names",
            default=[],
            help="Queue name to process. Can be specified multiple times. Use '*' to process all queues.",
        )
        parser.add_argument(
            "--exclude-queue",
            "-Q",
            action="append",
            dest="excluded_queue_names",
            default=[],
            help="Queue name to exclude. Can be specified multiple times.",
        )
        parser.add_argument(
            "--interval",
            "-i",
            type=valid_interval,
            default=1.0,
            help="Polling interval in seconds (default: 1.0).",
        )
        parser.add_argument(
            "--batch",
            action=BooleanOptionalAction,
            default=False,
            help="Run in batch mode: exit when no more tasks are available.",
        )
        parser.add_argument(
            "--backend",
            dest="backend_name",
            type=valid_backend_name,
            default=DEFAULT_TASK_BACKEND_ALIAS,
            help="Backend name to process tasks for.",
        )
        parser.add_argument(
            "--startup-delay",
            action=BooleanOptionalAction,
            default=True,
            help="Add a random startup delay to avoid thundering herd.",
        )
        parser.add_argument(
            "--max-tasks",
            type=valid_max_tasks,
            default=None,
            help="Maximum number of tasks to process before exiting.",
        )
        parser.add_argument(
            "--worker-id",
            default=None,
            help="Unique identifier for this worker.",
        )

    def handle(self, *args, **options) -> None:
        queue_names = options["queue_names"] or [DEFAULT_TASK_QUEUE_NAME]
        excluded_queue_names = options["excluded_queue_names"]
        interval = options["interval"]
        batch = options["batch"]
        backend_name = options["backend_name"]
        startup_delay = options["startup_delay"]
        max_tasks = options["max_tasks"]
        worker_id = options["worker_id"] or get_random_string(12)

        worker = Worker(
            queue_names=queue_names,
            interval=interval,
            batch=batch,
            backend_name=backend_name,
            startup_delay=startup_delay,
            max_tasks=max_tasks,
            worker_id=worker_id,
            excluded_queue_names=excluded_queue_names,
        )

        if DJANGO_AUTORELOAD_ENV not in os.environ:
            # The parent process should configure its signals
            worker.configure_signals()

            run_with_reloader(worker.run)
        else:
            worker.configure_signals()
            worker.run()
