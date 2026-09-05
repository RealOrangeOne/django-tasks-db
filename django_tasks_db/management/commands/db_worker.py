import logging
import math
import os
import random
import signal
import sys
import threading
import time
from argparse import ArgumentParser, ArgumentTypeError, BooleanOptionalAction
from types import FrameType

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections, models
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
    TaskResultStatus,
    task_backends,
    task_finished,
    task_started,
)
from django_tasks_db.models import DBTaskPing, DBTaskResult
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
        self.running_task: str | None = None
        self._run_tasks = 0
        self._lost_tasks: dict[tuple[str, ...], list[int]] = {}
        self._stopping = threading.Event()

        self.worker_id = worker_id

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

    def shutdown(self, signum: int, frame: FrameType | None) -> None:
        self._stopping.set()
        if not self.running:
            logger.warning(
                "Received %s - terminating current task.", signal.strsignal(signum)
            )
            self.reset_signals()
            self._close_connections()
            sys.exit(1)

        logger.warning(
            "Received %s - shutting down gracefully... (press Ctrl+C again to force)",
            signal.strsignal(signum),
        )
        self.running = False

    def _close_connections(self) -> None:
        close_old_connections()
        connections.close_all()

    def _limbo_tasks(self, /, queues: set[str], *, min_samples: int = 5) -> None:
        # Query for tasks that are in limbo
        running_tasks = DBTaskResult.objects.running().filter(
            backend_name=self.backend_name,
            queue_name__in=queues,
        )
        for t in running_tasks:
            for wid in t.worker_ids:
                # we were the only worker to attempt it
                if 1 == len(t.worker_ids) and wid == self.worker_id:
                    DBTaskResult.objects.filter(
                        id=t.id,
                        queue_name=t.queue_name,
                        backend_name=t.backend_name,
                        status=TaskResultStatus.RUNNING,
                    ).update(
                        status=TaskResultStatus.READY,
                    )
                    continue
                ping, created = DBTaskPing.objects.get_or_create(
                    task_id=t.id,
                    worker_id=wid,
                    queue_name=t.queue_name,
                    backend_name=t.backend_name,
                )
                k = tuple(map(str, (wid, t.id, t.queue_name, t.backend_name)))
                if created:
                    v = [0]
                else:
                    v = self._lost_tasks.get(k, [])
                    v.append(ping.pongs)
                self._lost_tasks[k] = v
        for k, v in self._lost_tasks.items():
            # skip over candidates with too few samples
            if min_samples >= len(v):
                continue
            wid, tid, qn, ben = k
            # no workers claimed this task as active
            if v[0] == v[-1]:
                DBTaskResult.objects.filter(
                    id=tid,
                    queue_name=qn,
                    backend_name=ben,
                    status=TaskResultStatus.RUNNING,
                ).update(
                    status=TaskResultStatus.READY,
                )
            else:
                v.clear()
            DBTaskPing.objects.filter(
                task_id=tid,
                queue_name=qn,
                backend_name=ben,
            ).delete()

    def _next_task_result(self) -> tuple[DBTaskResult | None, bool]:
        tasks = DBTaskResult.objects.ready().filter(backend_name=self.backend_name)
        # TODO: use self._resolve_queues() instead
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

        return task_result, retrieved_task_result

    def _ping_responder(self, /, queues: set[str]) -> None:
        close_old_connections()
        try:
            while not self._stopping.wait(self.interval):
                try:
                    pings = DBTaskPing.objects.filter(
                        worker_id=self.worker_id,
                        backend_name=self.backend_name,
                        queue_name__in=queues,
                    )
                    if self.running_task is not None:
                        pings.filter(task_id=self.running_task).update(
                            pongs=1 + models.F("pongs")
                        )

                # ruff: ignore[F841,S110]
                except BaseException as e:
                    pass
                    # tests expecting output may need to be adjusted first
                    # logger.debug(f"[ping-responder] {e!s}")
                finally:
                    close_old_connections()
        finally:
            self._stopping.set()
            self._close_connections()

    def _resolve_queues(self) -> set[str]:
        queues = set(self.queue_names)
        if self.process_all_queues:
            queues.update(task_backends[self.backend_name].queues)
            queues.difference_update(self.excluded_queue_names)
            queues.discard("*")
        return queues

    def run(self) -> None:
        logger.info(
            "Starting worker worker_id=%s queues=%s",
            self.worker_id,
            ",".join(self.queue_names),
        )

        queues = self._resolve_queues()
        thread_name_prefix = f"{self.backend_name}-tasks-db_worker-{self.worker_id}"
        self._pong_thread = threading.Thread(
            target=self._ping_responder,
            name=f"{thread_name_prefix}-ping-responder-{','.join(sorted(queues))}",
            # ruff: ignore[C408]
            kwargs=dict(queues=queues),
            daemon=False,
        )

        if self.startup_delay and self.interval:
            # Add a random small delay before starting to avoid a thundering herd
            time.sleep(random.random())  # noqa: S311

        self._pong_thread.start()
        try:
            while self.running and not self._stopping.is_set():
                # Check for dropped/expired connections right after waking up
                close_old_connections()

                task_result, retrieved_task_result = self._next_task_result()

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

                self._limbo_tasks(queues=queues)

                # Emulate Django's request behaviour and check for expired
                # database connections periodically.
                close_old_connections()

                self._stopping.wait(self.interval)

        finally:
            self._stopping.set()
            self.running = False
            self._pong_thread.join()
            self._close_connections()

    def run_task(self, db_task_result: DBTaskResult) -> None:
        """
        Run the given task, marking it as successful or failed.
        """
        try:
            self.running_task = str(db_task_result.id)
            task = db_task_result.task
            task_result = db_task_result.task_result

            backend_type = task.get_backend()

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
            task_finished.send(
                sender=backend_type, task_result=db_task_result.task_result
            )
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
        finally:
            self.running_task = None
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
    if num <= 0:
        raise ArgumentTypeError("Must be greater than zero")
    return num


def validate_worker_id(val: str) -> str:
    if not val:
        raise ArgumentTypeError("Worker id must not be empty")
    if len(val) > 64:
        raise ArgumentTypeError("Worker ids must be shorter than 64 characters")
    return val


class Command(BaseCommand):
    help = "Run a database background worker"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--queue-name",
            nargs="?",
            default=DEFAULT_TASK_QUEUE_NAME,
            type=str,
            help="The queues to process. Separate multiple with a comma. To process all queues, use '*' (default: %(default)r)",
        )
        parser.add_argument(
            "--exclude-queues",
            nargs="?",
            default="",
            type=str,
            help="Queues to exclude. Separate multiple with a comma.",
        )
        parser.add_argument(
            "--interval",
            nargs="?",
            default=1,
            type=valid_interval,
            help="The interval (in seconds) to wait, when there are no tasks in the queue, before checking for tasks again (default: %(default)r)",
        )
        parser.add_argument(
            "--batch",
            action="store_true",
            help="Process all outstanding tasks, then exit. Can be used in combination with --max-tasks.",
        )
        parser.add_argument(
            "--reload",
            action=BooleanOptionalAction,
            default=settings.DEBUG,
            help="Reload the worker on code changes. Not recommended for production as tasks may not be stopped cleanly (default: DEBUG)",
        )
        parser.add_argument(
            "--backend",
            nargs="?",
            default=DEFAULT_TASK_BACKEND_ALIAS,
            type=valid_backend_name,
            dest="backend_name",
            help="The backend to operate on (default: %(default)r)",
        )
        parser.add_argument(
            "--no-startup-delay",
            action="store_false",
            dest="startup_delay",
            help="Don't add a small delay at startup.",
        )
        parser.add_argument(
            "--max-tasks",
            nargs="?",
            default=None,
            type=valid_max_tasks,
            help="If provided, the maximum number of tasks the worker will execute before exiting.",
        )
        parser.add_argument(
            "--worker-id",
            nargs="?",
            type=validate_worker_id,
            help="Worker id. MUST be unique across worker pool (default: auto-generate)",
            default=get_random_string(32),
        )

    def configure_logging(self, verbosity: int) -> None:
        tasks_logger = logging.getLogger(TASKS_LOGGER)

        if verbosity == 0:
            tasks_logger.setLevel(logging.CRITICAL)
            logger.setLevel(logging.CRITICAL)
        elif verbosity == 1:
            tasks_logger.setLevel(logging.INFO)
            logger.setLevel(logging.INFO)
        else:
            tasks_logger.setLevel(logging.DEBUG)
            logger.setLevel(logging.DEBUG)

        # If no handler is configured, the logs won't show,
        # regardless of the set level.
        if not tasks_logger.hasHandlers():
            tasks_logger.addHandler(logging.StreamHandler(self.stdout))

        if not logger.hasHandlers():
            logger.addHandler(logging.StreamHandler(self.stdout))

    def handle(
        self,
        *,
        verbosity: int,
        queue_name: str,
        interval: float,
        batch: bool,
        backend_name: str,
        startup_delay: bool,
        reload: bool,
        max_tasks: int | None,
        worker_id: str,
        exclude_queues: str,
        **options: dict,
    ) -> None:
        self.configure_logging(verbosity)

        if reload and batch:
            logger.warning(
                "Warning: --reload and --batch cannot be specified together. Disabling autoreload."
            )
            reload = False

        queue_names = queue_name.split(",")
        excluded_queue_names = exclude_queues.split(",") if exclude_queues else []

        if excluded_queue_names and "*" not in queue_names:
            raise CommandError("--exclude-queues can only be used with --queue-name=*")

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

        if reload:
            if os.environ.get(DJANGO_AUTORELOAD_ENV) == "true":
                # Only the child process should configure its signals
                worker.configure_signals()

            run_with_reloader(worker.run)
        else:
            worker.configure_signals()
            worker.run()
