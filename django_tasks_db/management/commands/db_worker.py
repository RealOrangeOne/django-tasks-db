import logging
import math
import os
import random
import signal
import sys
import threading
import time
from argparse import ArgumentParser, ArgumentTypeError, BooleanOptionalAction
from dataclasses import dataclass
from types import FrameType
from typing import TypeAlias

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

TaskKey: TypeAlias = tuple[str, str]
LostTaskKey: TypeAlias = tuple[str, str, str]


@dataclass(slots=True)
class _LostTaskSamples:
    count: int
    first: int
    last: int

    @property
    def claimed(self) -> bool:
        return self.first != self.last

    def add(self, pongs: int) -> None:
        self.count += 1
        self.last = pongs


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
        self._stopping = threading.Event()
        self._running_task_lock = threading.Lock()
        self._lost_tasks: dict[LostTaskKey, _LostTaskSamples] = {}

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

    def _task_key(self, task: DBTaskResult) -> TaskKey:
        return (str(task.id), str(task.queue_name))

    def _lost_task_key(
        self,
        worker_id: str,
        task: DBTaskResult,
    ) -> LostTaskKey:
        return (worker_id,) + self._task_key(task)

    def _is_this_worker_limbo_task(self, task: DBTaskResult) -> bool:
        task_id = str(task.id)
        worker_ids = set(map(str, task.worker_ids))
        # Snapshot the running_task state
        with self._running_task_lock:
            running_task = self.running_task
        return (
            1 == len(worker_ids)
            and tuple(worker_ids)[0] == self.worker_id
            and task_id != running_task
        )

    def _track_task_pings(self, task: DBTaskResult) -> None:
        if self._is_this_worker_limbo_task(task):
            return

        for worker_id in set(map(str, task.worker_ids)):
            key = self._lost_task_key(worker_id, task)
            samples = self._lost_tasks.get(key)

            if samples is not None:
                ping = (
                    DBTaskPing.objects.only("pongs")
                    .filter(
                        worker_id=worker_id,
                        queue_name=task.queue_name,
                        backend_name=self.backend_name,
                        task_id=task.id,
                    )
                    .first()
                )
                if ping:
                    samples.add(ping.pongs)
                    continue

            ping, created = DBTaskPing.objects.get_or_create(
                worker_id=worker_id,
                queue_name=task.queue_name,
                backend_name=self.backend_name,
                task_id=task.id,
            )

            self._lost_tasks[key] = _LostTaskSamples(
                count=1,
                first=0 if created else ping.pongs,
                last=0 if created else ping.pongs,
            )

    def _clear_task_tracking(self, task: DBTaskResult) -> None:
        DBTaskPing.objects.filter(
            task_id=task.id,
            queue_name=task.queue_name,
            backend_name=self.backend_name,
        ).delete()

        task_id = str(task.id)
        queue_name = str(task.queue_name)

        for key in tuple(self._lost_tasks):
            worker_id, tracked_task_id, tracked_queue_name = key

            if tracked_task_id == task_id and tracked_queue_name == queue_name:
                self._lost_tasks.pop(key, None)

    def _clean_missing_tasks(self, running_task_keys: set[TaskKey]) -> None:
        missing_task_keys: set[TaskKey] = set()
        for key in tuple(self._lost_tasks):
            if key[1:] not in running_task_keys:
                missing_task_keys.add(key[1:])
                self._lost_tasks.pop(key, None)

        chunk = {
            missing_task_keys.pop() for _ in range(min(100, len(missing_task_keys)))
        }
        while chunk:
            task_ids_by_queue_name: dict[str, set[str]] = {}
            for task_id, queue_name in chunk:
                task_ids = task_ids_by_queue_name.get(queue_name, set())
                task_ids.add(task_id)
                task_ids_by_queue_name[queue_name] = task_ids
            for queue_name, task_ids in task_ids_by_queue_name.items():
                DBTaskPing.objects.filter(
                    task_id__in=task_ids,
                    queue_name=queue_name,
                    backend_name=self.backend_name,
                ).delete()
            chunk = {
                missing_task_keys.pop() for _ in range(min(100, len(missing_task_keys)))
            }

    def _mark_task_ready(self, task: DBTaskResult) -> None:
        DBTaskResult.objects.running().filter(
            id=task.id,
            queue_name=task.queue_name,
            backend_name=task.backend_name,
        ).update(
            status=TaskResultStatus.READY,
        )

    def _limbo_tasks(self, /, queues: set[str], *, min_samples: int = 6) -> None:
        running_tasks = tuple(
            DBTaskResult.objects.running().filter(
                backend_name=self.backend_name,
                queue_name__in=queues,
            )
        )
        running_task_keys = {self._task_key(task) for task in running_tasks}

        self._clean_missing_tasks(running_task_keys)

        for task in running_tasks:
            worker_ids = set(map(str, task.worker_ids))
            if not worker_ids:
                continue

            if self._is_this_worker_limbo_task(task):
                self._clear_task_tracking(task)
                self._mark_task_ready(task)
                continue

            self._track_task_pings(task)
            samples = tuple(
                self._lost_tasks[lost_task_key]
                for worker_id in worker_ids
                if (lost_task_key := self._lost_task_key(worker_id, task))
                in self._lost_tasks
            )

            if len(samples) != len(worker_ids):
                # A worker assignment is missing from the tracking state.
                continue

            if any(min_samples > sample.count for sample in samples):
                continue

            task_claimed = any(sample.claimed for sample in samples)

            self._clear_task_tracking(task)

            if not task_claimed:
                self._mark_task_ready(task)

    def _next_task_result(self) -> tuple[DBTaskResult | None, bool]:
        tasks = DBTaskResult.objects.ready().filter(
            backend_name=self.backend_name,
            queue_name__in=self._resolve_queues(),
        )

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
        local = threading.local()
        # ruff: ignore[S311]
        local.interval = random.triangular(low=0.25, high=2.0, mode=2)
        close_old_connections()
        try:
            while not self._stopping.wait(local.interval):
                # ruff: ignore[S311]
                local.interval = random.triangular(low=0.25, high=2.0, mode=2)
                try:
                    pings = DBTaskPing.objects.filter(
                        worker_id=self.worker_id,
                        queue_name__in=queues,
                        backend_name=self.backend_name,
                    )
                    # Snapshot the running_task state
                    with self._running_task_lock:
                        running_task = self.running_task
                    if running_task is not None:
                        pings.filter(task_id=running_task).update(
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

                if self.running and task_result is None:
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
            with self._running_task_lock:
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
            with self._running_task_lock:
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
