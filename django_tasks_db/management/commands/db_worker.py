import logging
import math
import os
import queue
import random
import signal
import sys
import time
from argparse import ArgumentParser, ArgumentTypeError, BooleanOptionalAction
from queue import Empty, Full, SimpleQueue, Queue
from threading import Event, Thread
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


def get_resolved_queue_names(
    backend_name: str,
    queue_names: list[str],
    excluded_queue_names: list[str]
) -> tuple[str, ...]:
    """
    Translates '*' to the complete collection of configured backend queues by
    inspecting the task_backends instance registry directly, then strips exclusions.
    """
    resolved = set(queue_names)

    if "*" in resolved:
        backend_instance = task_backends[backend_name]
        configured_queues = getattr(backend_instance, "queue_names", [])

        if not configured_queues and hasattr(backend_instance, "queues"):
            configured_queues = list(backend_instance.queues.keys())

        resolved.remove("*")
        resolved.update(configured_queues)

    resolved.difference_update(excluded_queue_names)
    return tuple(sorted(resolved))


class Worker:
    def __init__(
        self,
        *,
        queue_names: tuple[str, ...],
        interval: float,
        batch: bool,
        backend_name: str,
        startup_delay: bool,
        max_tasks: int | None,
        worker_id: str,
        num_threads: int = 1,
        blip_budget: int = 5,
    ):
        self.queue_names = queue_names
        self.interval = interval
        self.batch = batch
        self.backend_name = backend_name
        self.startup_delay = startup_delay
        self.max_tasks = max_tasks

        self.running = True
        self._run_tasks = 0

        self.worker_id = worker_id

        self.monitor_running_event = Event()
        self.monitor_running_event.set()
        self.blip_budget = blip_budget
        self.consecutive_blips = 0

        self.monitor_thread: Thread | None = None
        self.num_threads = num_threads
        self.task_runner_threads: list[Thread] = []
        self.consumer_threads: dict[str, Thread] = {}

        # Unique Signaling Channels: queue_name -> (SimpleQueue, stopping_event)
        self.queues: dict[str, tuple[SimpleQueue, Event]] = {}

        # Recovery Synchronization State Trackers
        self.startup_recovery_triggered = False
        self.recovery_responses: dict[str, set[str]] = {}

        # Decoupled Compute & Feedback Channels
        self.task_data_queue: queue.Queue = queue.Queue()
        self.monitor_feedback_queue: SimpleQueue = SimpleQueue()

    def _shutdown():
        self.running = False
        self.monitor_running_event.clear()
        for msg_queue, stop_event in self.queues.values():
            stop_event.set()
            try:
                msg_queue.put_nowait("SHUTDOWN")
            except Full:
                pass

    def shutdown(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        """Main orchestrator thread clears the running event and waits for cleanup."""
        if not self.running:
            logger.warning(
                "Received %s - terminating current task.", signal.strsignal(signum)
            )
            self.reset_signals()
            sys.exit(1)

        if signum:
            logger.warning(
                "Received %s - shutting down gracefully... (press Ctrl+C again to force)",
                signal.strsignal(signum),
            )
        else:
            logger.critical("Emergency exit: Monitor thread exhausted its database blip budget.")

        self._shutdown()

        sys.exit(0 if signum else 1)

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
        thread_name_prefix = f"{self.backend_name}-tasks-db_worker-{self.worker_id}"
        logger.info(
            "Starting worker worker_id=%s queues=%s",
            self.worker_id,
            ",".join(self.queue_names),
        )

        # Establish isolated pure-signaling consumers
        for q_name in self.queue_names:
            msg_queue = SimpleQueue()
            stop_event = Event()
            self.queues[q_name] = (msg_queue, stop_event)

            consumer = Thread(
                target=self._consumer_signaling_loop,
                args=(q_name, msg_queue, stop_event, self.monitor_feedback_queue),
                name=f"{thread_name_prefix}@{q_name}",
                daemon=True,
            )
            self.consumer_threads[q_name] = consumer
            consumer.start()

        # Establish task runner pool
        for n in range(self.num_threads):
            runner = Thread(
                target=self._task_runner_compute_loop,
                args=(self.task_data_queue, self.queues, self.monitor_feedback_queue),
                name=f"{thread_name_prefix}-task-{1+n}",
                daemon=True,
            )
            self.task_runner_threads.append(runner)
            runner.start()

        if self.startup_delay and self.interval:
            # Add a random small delay before starting to avoid a thundering herd
            time.sleep(random.random())  # noqa: S311

        self.monitor_thread = Thread(
            target=self._monitor_dispatcher_loop,
            name=f"{thread_name_prefix}-database",
            daemon=False,
        )
        self.monitor_thread.start()

        while self.running:
            if self.max_tasks is not None and self._run_tasks >= self.max_tasks:
                logger.info(
                    "Ran maximum tasks (%d) on worker=%s - exiting gracefully.",
                    self._run_tasks,
                    self.worker_id,
                )
                self._shutdown()
                continue

            if not self.monitor_thread.is_alive():
                logger.critical("Critical error: Persistent database thread crashed. Stopping worker.")
                self._shutdown()
                continue

            time.sleep(self.interval)

    def _monitor_dispatcher_loop(self) -> None:
        """The absolute ONLY location touching the Django ORM. Loops on event state status."""
        logger.debug("Persistent ORM monitor loop started.")
        close_old_connections()

        while self.monitor_running_event.is_set():
            try:
                # Phase A: Block and listen on the feedback SimpleQueue using the interval timeout parameter
                try:
                    feedback = self.monitor_feedback_queue.get(timeout=self.interval)

                    while True:
                        match feedback:
                            case ("SIGNAL_ACK", q_name, details):
                                logger.info("Queue [%s] Ping Response -> %s", q_name, details)

                            case ("RECOVERY_CHECK_ACK", q_name, task_id, is_active):
                                if task_id in self.recovery_responses:
                                    if is_active:
                                        self.recovery_responses[task_id].add("__ACTIVE__")

                                    self.recovery_responses[task_id].add(q_name)

                                    if len(self.queue_names) == len(self.recovery_responses[task_id] - {"__ACTIVE__"}):
                                        responses = self.recovery_responses.pop(task_id)

                                        if "__ACTIVE__" not in responses:
                                            logger.warning("Task ID %s verified as LOST across all local queues. Resetting database state...", task_id)
                                            try:
                                                stuck_task = DBTaskResult.objects.get(id=task_id)
                                                stuck_task.worker_id = None
                                                stuck_task.status = "ready"
                                                stuck_task.save(update_fields=["worker_id", "status"])
                                            except Exception:
                                                logger.exception("Failed to reset database parameters for lost task id=%s", task_id)
                                        else:
                                            logger.debug("Task ID %s is safely executing inside a local compute thread.", task_id)

                            case ("TASK_SUCCESS", db_task_id, return_val):
                                try:
                                    res = DBTaskResult.objects.get(id=db_task_id)
                                    res.set_successful(return_val)
                                except Exception:
                                    logger.exception("Failed to write back success for task id=%s", db_task_id)
                                self._run_tasks += 1
                                close_old_connections()
                            case ("TASK_FAILURE", db_task_id, error_instance):
                                try:
                                    res = DBTaskResult.objects.get(id=db_task_id)
                                    res.set_failed(error_instance)
                                except Exception:
                                    logger.exception("Failed to record task failure for id=%s", db_task_id)
                                self._run_tasks += 1
                                close_old_connections()

                        try:
                            feedback = self.monitor_feedback_queue.get_nowait()
                        except Empty:
                            break
                except Empty:
                    pass

                if not self.monitor_running_event.is_set():
                    continue

                # Phase B: One-time recovery sweep using our pre-resolved queues tuple
                if not self.startup_recovery_triggered:
                    self.startup_recovery_triggered = True
                    close_old_connections()

                    stuck_candidates = DBTaskResult.objects.filter(
                        backend_name=self.backend_name,
                        status="running"
                    )
                    if self.queue_names:
                        stuck_candidates = stuck_candidates.filter(queue_name__in=self.queue_names)

                    for candidate in stuck_candidates:
                        if candidate.id not in self.recovery_responses:
                            self.recovery_responses[candidate.id] = set()
                            logger.info("Startup Audit: Broadcasting verification to locate potential lost task ID: %s", candidate.id)

                            for q_name, (msg_queue, _) in self.queues.items():
                                msg_queue.put(("AUDIT_LOST_TASK", candidate.id))

                # Phase C: Query the database for standard new ready background tasks
                tasks = DBTaskResult.objects.ready().filter(backend_name=self.backend_name)
                if self.queue_names:
                    tasks = tasks.filter(queue_name__in=self.queue_names)

                task_result = None
                retrieved_task = False

                with exclusive_transaction(tasks.db):
                    try:
                        task_result = tasks.get_locked()
                        retrieved_task = True
                        if task_result is not None:
                            task_result.claim(self.worker_id)
                    except OperationalError as e:
                        retrieved_task = False
                        if not is_locked_database_exception(e):
                            raise

                self.consecutive_blips = 0

                if task_result is not None:
                    close_old_connections()
                    self.task_data_queue.put(task_result)

                if self.batch and retrieved_task and task_result is None:
                    logger.info(
                        "No more tasks to run for worker_id=%s - exiting gracefully.",
                        self.worker_id,
                    )
                    self.monitor_running_event.clear()
                    continue

            except (OperationalError, Exception) as err:
                self.consecutive_blips += 1
                logger.error("Monitor thread encountered database error (%d/%d): %s", self.consecutive_blips, self.blip_budget, err)
                try:
                    close_old_connections()
                except Exception:
                    pass

                if self.consecutive_blips >= self.blip_budget:
                    self.monitor_running_event.clear()
                    continue

        if self.consecutive_blips >= self.blip_budget:
            logger.critical("Emergency exit: Monitor thread exhausted its database blip budget.")
        else:
            logger.info("Monitor thread loop exited. Distributing final channel shutdowns...")
        self._shutdown()
        logger.debug("Monitor dispatcher thread gracefully exited.")

    def _consumer_signaling_loop(
        self,
        queue_name: str,
        msg_queue: SimpleQueue,
        stopping_event: Event,
        feedback_queue: SimpleQueue
    ) -> None:
        """Signaling channel loop using SimpleQueue mapping with a 0.5s timeout."""
        logger.debug("Signaling monitor active for: %s", queue_name)

        # Local metrics and identifiers isolated completely to this thread context
        import threading
        local = threading.local()
        local.current_task_id = None
        local.total_tasks_run = 0

        while not stopping_event.is_set():
            try:
                msg = msg_queue.get(timeout=0.5)

                match msg:
                    case "SHUTDOWN":
                        stopping_event.set()
                        continue

                    case ("TASK_STARTED", task_id):
                        local.current_task_id = task_id
                        logger.debug("Queue [%s] registered active execution for local task ID: %s", queue_name, task_id)

                    case ("TASK_FINISHED", task_id):
                        if task_id == local.current_task_id:
                            local.current_task_id = None
                        local.total_tasks_run += 1
                        logger.debug("Queue [%s] cleared execution for local task ID: %s", queue_name, task_id)

                    case ("AUDIT_LOST_TASK", target_id):
                        is_active_here = (target_id == local.current_task_id)
                        feedback_queue.put(("RECOVERY_CHECK_ACK", queue_name, target_id, is_active_here))

                    case "QUERY_CURRENT_TASK":
                        feedback_queue.put(("SIGNAL_ACK", queue_name, f"CURRENT_TASK_ID:{local.current_task_id}"))

                    case "PING":
                        feedback_payload = f"CURRENT_TASK_ID:{local.current_task_id}|TOTAL_TASKS_RUN:{local.total_tasks_run}"
                        feedback_queue.put(("SIGNAL_ACK", queue_name, feedback_payload))

                    case _:
                        feedback_queue.put(("SIGNAL_ACK", queue_name, msg))
            except Empty:
                continue

    def _task_runner_compute_loop(self, task_data_queue: Queue, signaling_queues: dict, feedback_queue: SimpleQueue) -> None:
        """Pure compute environment worker threads. Emits task lifecycle signals directly from here."""
        logger.debug("Compute worker thread initialization complete.")
        while self.monitor_running_event.is_set():
            task_retrieved = False
            task_item = None

            try:
                task_item = task_data_queue.get(timeout=1.0)
                task_retrieved = True

                if isinstance(task_item, DBTaskResult):
                    q_name = task_item.queue_name
                    task_id = task_item.id

                    if q_name in signaling_queues:
                        signaling_queues[q_name].put(("TASK_STARTED", task_id))

                    backend_type_assigned = False
                    try:
                        task = task_item.task
                        task_result = task_item.task_result
                        backend_type = task.get_backend()
                        backend_type_assigned = True

                        task_started.send(sender=backend_type, task_result=task_result)

                        if task.takes_context:
                            return_value = task.call(
                                TaskContext(task_result=task_result),
                                *task_result.args,
                                **task_result.kwargs,
                            )
                        else:
                            return_value = task.call(*task_result.args, **task_result.kwargs)

                        feedback_queue.put(("TASK_SUCCESS", task_id, return_value))
                    except BaseException as ex:
                        feedback_queue.put(("TASK_FAILURE", task_id, ex))
                        backend_type = type(backend_type)
                    finally:
                        if backend_type_assigned:
                            task_finished.send(sender=backend_type, task_result=task_result)
                        if q_name in signaling_queues:
                            signaling_queues[q_name].put(("TASK_FINISHED", task_id))

            except Empty:
                continue
            finally:
                if task_retrieved:
                    task_data_queue.task_done()

        logger.debug("Compute worker thread gracefully exited.")

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


def valid_thread_count(val: str) -> int:
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
            "--threads",
            nargs="?",
            default=1,
            type=valid_thread_count,
            dest="num_threads",
            help="The number of task execution threads to spawn (default: 1)",
        )
        parser.add_argument(
            "--worker-id",
            nargs="?",
            type=validate_worker_id,
            help="Worker id. MUST be unique across worker pool (default: auto-generate)",
            default=None,
        )

    def configure_logging(self, verbosity: int) -> None:
        tasks_logger = logging.getLogger(TASKS_LOGGER)

        match verbosity:
            case 0:
                tasks_logger.setLevel(logging.CRITICAL)
                logger.setLevel(logging.CRITICAL)
            case 1:
                tasks_logger.setLevel(logging.INFO)
                logger.setLevel(logging.INFO)
            case _:
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
        num_threads: int,
        worker_id: str | None,
        exclude_queues: str,
        **options: dict,
    ) -> None:
        self.configure_logging(verbosity)

        resolved_worker_id = get_random_string(32) if worker_id is None else worker_id

        if reload and batch:
            logger.warning(
                "Warning: --reload and --batch cannot be specified together. Disabling autoreload."
            )
            reload = False

        raw_queue_names = queue_name.split(",")
        excluded_queue_names = exclude_queues.split(",") if exclude_queues else []

        if excluded_queue_names and "*" not in raw_queue_names:
            raise CommandError("--exclude-queues can only be used with --queue-name=*")

        resolved_queues = get_resolved_queue_names(backend_name, raw_queue_names, excluded_queue_names)

        worker = Worker(
            queue_names=resolved_queues,
            interval=interval,
            batch=batch,
            backend_name=backend_name,
            startup_delay=startup_delay,
            max_tasks=max_tasks,
            worker_id=resolved_worker_id,
            num_threads=num_threads,
        )

        if reload:
            if "true" == os.environ.get(DJANGO_AUTORELOAD_ENV):
                # Only the child process should configure its signals
                worker.configure_signals()

            run_with_reloader(worker.run)
        else:
            worker.configure_signals()
            worker.run()
