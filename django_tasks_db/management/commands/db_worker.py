import logging
import math
import os
import queue
import random
import signal
import sys
import threading
import time
from argparse import ArgumentParser, ArgumentTypeError, BooleanOptionalAction
from queue import Empty, SimpleQueue
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
        excluded_queue_names: list[str],
        num_threads: int = 1,
        blip_budget: int = 5,
    ):
        self.queue_names = queue_names
        self.process_all_queues = 0 == len(queue_names) or any(q in queue_names for q in ("*", ""))
        self.excluded_queue_names = excluded_queue_names
        self.interval = interval
        self.batch = batch
        self.backend_name = backend_name
        self.startup_delay = startup_delay
        self.max_tasks = max_tasks
        self.worker_id = worker_id
        self.num_threads = num_threads
        
        self.blip_budget = blip_budget
        self.consecutive_blips = 0

        self.running = True
        self._run_tasks = 0

        # Master Thread Control: Running event that is CLEARED to signal stop
        self.monitor_running_event = threading.Event()
        self.monitor_running_event.set() 
        
        self.monitor_thread: threading.Thread | None = None
        self.task_runner_threads: list[threading.Thread] = []
        self.consumer_threads: dict[str, threading.Thread] = {}
        
        # Unique Signaling Channels: queue_name -> (SimpleQueue, stopping_event)
        self.queues: dict[str, tuple[SimpleQueue, threading.Event]] = {}
        
        # Recovery Synchronization State Trackers
        self.startup_recovery_triggered = False
        self.recovery_responses: dict[str, set[str]] = {} 
        
        # Decoupled Compute & Feedback Channels
        self.task_data_queue: queue.Queue = queue.Queue()
        self.monitor_feedback_queue: SimpleQueue = SimpleQueue()

    def shutdown(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        """Main orchestrator thread clears the running event and waits for cleanup."""
        if not self.running:
            self.reset_signals()
            sys.exit(1)

        if signum:
            logger.warning("Received signal %s - structural teardown triggered...", signal.strsignal(signum))
        else:
            logger.critical("Emergency exit: Monitor thread exhausted its database blip budget.")

        self.running = False
        self.monitor_running_event.clear() 

        # Wait for the monitor thread to execute its clean exit signaling loop
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=2.0)

        # Join signaling loops safely
        for thread in self.consumer_threads.values():
            thread.join(timeout=1.0)
            
        # Join execution thread tracks safely
        for thread in self.task_runner_threads:
            thread.join(timeout=2.0)

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
        logger.info("Spawning isolated multi-threaded engine worker_id=%s", self.worker_id)

        if self.startup_delay and self.interval:
            time.sleep(random.random())

        # 1. Establish isolated pure-signaling consumer loops
        for q_name in self.queue_names:
            msg_queue = SimpleQueue()
            stop_event = threading.Event()
            self.queues[q_name] = (msg_queue, stop_event)
            
            consumer = threading.Thread(
                target=self._consumer_signaling_loop,
                args=(q_name, msg_queue, stop_event, self.monitor_feedback_queue),
                daemon=True,
            )
            self.consumer_threads[q_name] = consumer
            consumer.start()

        # 2. Establish task runner pool based on configurable num_threads bounds
        for _ in range(self.num_threads):
            runner = threading.Thread(
                target=self._task_runner_compute_loop, 
                args=(self.monitor_feedback_queue, self.queues), 
                daemon=True
            )
            self.task_runner_threads.append(runner)
            runner.start()

        # 3. Spawn the sole database-bound supervisor monitor thread (NOT a daemon thread)
        self.monitor_thread = threading.Thread(target=self._monitor_dispatcher_loop, daemon=False)
        self.monitor_thread.start()

        # High-level orchestrator loop checks running statuses against the user interval value
        while self.running:
            if self.max_tasks is not None and self._run_tasks >= self.max_tasks:
                logger.info("Max task threshold reached (%d). Halting worker.", self._run_tasks)
                self.shutdown()
                return

            if not self.monitor_thread.is_alive():
                logger.critical("Critical error: Persistent monitor thread crashed. Stopping engine.")
                self.shutdown()
                return

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
                            case ("TASK_FAILURE", db_task_id, error_instance):
                                try:
                                    res = DBTaskResult.objects.get(id=db_task_id)
                                    res.set_failed(error_instance)
                                except Exception:
                                    logger.exception("Failed to record task failure for id=%s", db_task_id)
                                self._run_tasks += 1
                        
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
                    self.task_data_queue.put(task_result)
                
                if self.batch and retrieved_task and None is task_result:
                    logger.info("Batch criteria satisfied. Shutting down system execution.")
                    threading.Thread(target=self.shutdown, daemon=True).start()
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
                    threading.Thread(target=self.shutdown, daemon=True).start()
                    self.monitor_running_event.clear()
                    continue
            
        logger.info("Monitor thread loop exited. Distributing final channel shutdowns...")
        for q_name, (msg_queue, stop_event) in self.queues.items():
            stop_event.set()
            msg_queue.put("SHUTDOWN")
            
        logger.debug("Monitor dispatcher thread gracefully exited.")



#### old content below here ####

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
