import logging
import math
import os
import random
import signal
import sys
import threading
import time
from argparse import ArgumentParser, ArgumentTypeError, BooleanOptionalAction
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.utils import OperationalError
from django.utils import timezone
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
from django_tasks_db.models import DBTaskResult, DBWorkerHeartbeat
from django_tasks_db.utils import exclusive_transaction, is_locked_database_exception

logger = logging.getLogger("django_tasks_db")

DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_HEALTHCHECK_ADDRESS = "127.0.0.1"
DEFAULT_HEALTHCHECK_PORT = 8000


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
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        healthcheck: bool = False,
        healthcheck_address: str = DEFAULT_HEALTHCHECK_ADDRESS,
        healthcheck_port: int = DEFAULT_HEALTHCHECK_PORT,
    ):
        self.queue_names = queue_names
        self.process_all_queues = "*" in queue_names
        self.excluded_queue_names = excluded_queue_names
        self.interval = interval
        self.batch = batch
        self.backend_name = backend_name
        self.startup_delay = startup_delay
        self.max_tasks = max_tasks
        self.heartbeat_interval = heartbeat_interval
        self.healthcheck = healthcheck
        self.healthcheck_address = healthcheck_address
        self.healthcheck_port = healthcheck_port

        self.running = True
        self.running_task = False
        self._run_tasks = 0
        self._heartbeat_stop = threading.Event()
        self._heartbeat_error: Exception | None = None

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

        DBWorkerHeartbeat.objects.upsert(self.worker_id)
        heartbeat_thread = threading.Thread(
            target=self._update_heartbeat,
            name=f"heartbeat-{self.worker_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        healthcheck_server: ThreadingHTTPServer | None = None
        healthcheck_thread: threading.Thread | None = None
        try:
            if self.healthcheck:
                healthcheck_server, healthcheck_thread = (
                    self._start_healthcheck_server()
                )
            self._run_loop()
        finally:
            if healthcheck_server is not None and healthcheck_thread is not None:
                healthcheck_server.shutdown()
                healthcheck_server.server_close()
                healthcheck_thread.join()
            self._heartbeat_stop.set()
            heartbeat_thread.join()

    def _start_healthcheck_server(
        self,
    ) -> tuple[ThreadingHTTPServer, threading.Thread]:
        worker = self

        class HealthcheckHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/health":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                if worker._is_healthy():
                    status = HTTPStatus.OK
                    body = b"healthy\n"
                else:
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                    body = b"unhealthy\n"

                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format_string: str, *args: object) -> None:
                logger.debug("Healthcheck request: " + format_string, *args)

        server = ThreadingHTTPServer(
            (self.healthcheck_address, self.healthcheck_port),
            HealthcheckHandler,
        )
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"healthcheck-{self.worker_id}",
            daemon=True,
        )
        thread.start()
        logger.info(
            "Started healthcheck listener worker_id=%s address=%s port=%s",
            self.worker_id,
            *server.server_address[:2],
        )
        return server, thread

    def _is_healthy(self) -> bool:
        close_old_connections()
        try:
            cutoff = timezone.now() - timedelta(seconds=2 * self.heartbeat_interval)
            return DBWorkerHeartbeat.objects.filter(
                worker_id=self.worker_id,
                last_heartbeat_at__gte=cutoff,
            ).exists()
        except Exception:
            logger.exception(
                "Failed to check heartbeat for worker_id=%s", self.worker_id
            )
            return False
        finally:
            close_old_connections()

    def _run_loop(self) -> None:
        if self.startup_delay and self.interval:
            # Add a random small delay before starting to avoid a thundering herd
            time.sleep(random.random())  # noqa: S311

        while self.running:
            self._raise_heartbeat_error()
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
                self._raise_heartbeat_error()

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

    def _update_heartbeat(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_interval):
            close_old_connections()
            try:
                DBWorkerHeartbeat.objects.upsert(self.worker_id)
            except Exception as e:
                self._heartbeat_error = e
                self._heartbeat_stop.set()
                logger.exception(
                    "Failed to update heartbeat for worker_id=%s", self.worker_id
                )
                return
            finally:
                close_old_connections()

    def _raise_heartbeat_error(self) -> None:
        if self._heartbeat_error is not None:
            raise self._heartbeat_error

    def run_task(self, db_task_result: DBTaskResult) -> None:
        """
        Run the given task, marking it as successful or failed.
        """
        try:
            self.running_task = True
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


def valid_heartbeat_interval(val: str) -> float:
    num = valid_interval(val)
    if num == 0:
        raise ArgumentTypeError("Must be greater than zero")
    return num


def valid_healthcheck_port(val: str) -> int:
    try:
        port = int(val)
    except ValueError as e:
        raise ArgumentTypeError("Must be an integer port number") from e
    if not 0 <= port <= 65535:
        raise ArgumentTypeError("Must be between 0 and 65535")
    return port


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
        # Distinguish omitted verbosity from an explicit --verbosity=1.
        parser.set_defaults(verbosity=None)
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
            "--heartbeat-interval",
            nargs="?",
            default=DEFAULT_HEARTBEAT_INTERVAL,
            type=valid_heartbeat_interval,
            help="The interval (in seconds) between worker heartbeat updates (default: %(default)r)",
        )
        parser.add_argument(
            "--healthcheck",
            action="store_true",
            help="Start an HTTP healthcheck listener.",
        )
        parser.add_argument(
            "--healthcheck-address",
            default=DEFAULT_HEALTHCHECK_ADDRESS,
            help="Address for the healthcheck listener (default: %(default)s).",
        )
        parser.add_argument(
            "--healthcheck-port",
            type=valid_healthcheck_port,
            default=DEFAULT_HEALTHCHECK_PORT,
            help="Port for the healthcheck listener (default: %(default)s).",
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

    def configure_logging(self, verbosity: int | None) -> None:
        tasks_logger = logging.getLogger(TASKS_LOGGER)

        if verbosity == 0:
            log_level = logging.CRITICAL
        elif verbosity is None or verbosity == 1:
            log_level = logging.INFO
        else:
            log_level = logging.DEBUG

        for configured_logger in [tasks_logger, logger]:
            if verbosity is not None or configured_logger.level == logging.NOTSET:
                configured_logger.setLevel(log_level)

        # If no handler is configured, the logs won't show,
        # regardless of the set level.
        if not tasks_logger.hasHandlers():
            tasks_logger.addHandler(logging.StreamHandler(self.stdout))

        if not logger.hasHandlers():
            logger.addHandler(logging.StreamHandler(self.stdout))

    def handle(
        self,
        *,
        verbosity: int | None,
        queue_name: str,
        interval: float,
        batch: bool,
        backend_name: str,
        startup_delay: bool,
        reload: bool,
        max_tasks: int | None,
        worker_id: str,
        exclude_queues: str,
        heartbeat_interval: float,
        healthcheck: bool,
        healthcheck_address: str,
        healthcheck_port: int,
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
            heartbeat_interval=heartbeat_interval,
            healthcheck=healthcheck,
            healthcheck_address=healthcheck_address,
            healthcheck_port=healthcheck_port,
        )

        if reload:
            if os.environ.get(DJANGO_AUTORELOAD_ENV) == "true":
                # Only the child process should configure its signals
                worker.configure_signals()

            run_with_reloader(worker.run)
        else:
            worker.configure_signals()
            worker.run()
