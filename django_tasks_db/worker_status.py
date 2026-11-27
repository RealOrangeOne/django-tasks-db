import logging
import socket
import sys
from contextlib import ExitStack
from pathlib import Path

from django.core.files import locks

logger = logging.getLogger("django_tasks_db")


class SDNotify:
    """
    Send sd_notify(3) messages to systemd.

    Inspired by systemd_client.
    """

    def __init__(self, socket_path: str) -> None:
        if socket_path.startswith("@"):
            self.socket_address = "\0" + socket_path[1:]
        else:
            self.socket_address = socket_path

    def _send(self, message: bytes) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(self.socket_address)
                sock.sendall(message)
            return True
        except OSError:
            logger.exception("Failed to send to sd_notify socket")
            return False

    def ready(self) -> bool:
        return self._send(b"READY=1")

    def status(self, text: str) -> bool:
        return self._send(b"STATUS=" + text.encode())

    def stopping(self) -> bool:
        return self._send(b"STOPPING=1")

    def watchdog(self) -> bool:
        return self._send(b"WATCHDOG=1")


class WorkerStatus:
    """
    A class to help db_worker report its status.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        lock_file: Path | None = None,
        sd_socket_path: str | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.lock_file = lock_file

        self.running_stack = ExitStack()

        self.sd_notify = (
            SDNotify(sd_socket_path) if sd_socket_path is not None else None
        )

    def mark_started(self) -> bool:
        """
        Report the worker has started up and is about to check for tasks to execute.
        """
        if self.lock_file is not None:
            f = self.running_stack.enter_context(self.lock_file.open("ab"))
            # Grab an exclusive lock (if possible)
            if not locks.lock(f, locks.LOCK_EX | locks.LOCK_NB):
                return False

        if self.sd_notify is not None:
            self.sd_notify.ready()

        return True

    def ping(self) -> bool:
        """
        Report that the worker is still alive (has checked for a task).
        """
        if self.lock_file is not None:
            self.lock_file.touch()

        if self.sd_notify is not None:
            self.sd_notify.watchdog()

        return True

    def mark_running_task(self, task_id: str) -> bool:
        """
        Report that the worker is currently running a task.
        """
        if self.lock_file is not None and sys.platform != "win32":
            self.lock_file.write_text(task_id)

        if self.sd_notify is not None:
            self.sd_notify.status(f"Running {task_id}")

        return True

    def mark_not_running_task(self) -> bool:
        """
        Report that the worker is not running a task anymore.
        """
        if self.sd_notify is not None:
            self.sd_notify.status("")

        return True

    def mark_stopped(self) -> bool:
        """
        Report the worker has stopped (gracefully).
        """
        self.running_stack.close()

        if self.lock_file is not None:
            # Clean up the lock file
            self.lock_file.unlink()

        if self.sd_notify is not None:
            self.sd_notify.stopping()

        return True
