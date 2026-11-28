import logging
import os
import socket
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

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
        assert hasattr(socket, "AF_UNIX")  # noqa: S101

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


class S6Notify:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def ready(self) -> bool:
        try:
            os.write(self.fd, b"\n")
            return True
        except OSError:
            logger.exception("Failed to send S6 ready notification")
            return False
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass


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
        s6_notify_fd: int | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.lock_file = lock_file
        self.task_file = None

        self.running_stack = ExitStack()

        if lock_file is not None:
            temp_file = self.running_stack.enter_context(
                self._temp_file(suffix=".tid"),
            )
            self.task_file = Path(temp_file.name)
            temp_file.close()

        self.sd_notify = None if sd_socket_path is None else SDNotify(sd_socket_path)

        self.s6_notify = None if s6_notify_fd is None else S6Notify(s6_notify_fd)

    @contextmanager
    def _temp_file(
        self, suffix: str | None = None
    ) -> Iterator[tempfile._TemporaryFileWrapper]:
        lock_file = self.lock_file
        if lock_file is None:
            raise RuntimeError("Cannot create a temporary file without a lock file")

        prefix = lock_file.with_suffix("").name + "."
        lock_file.parent.mkdir(exist_ok=True, parents=True)
        try:
            temp_file = NamedTemporaryFile(  # type: ignore[call-overload, unused-ignore]
                delete=True,
                delete_on_close=False,
                dir=lock_file.parent,
                prefix=prefix,
                suffix=suffix,
            )
        except TypeError:
            temp_file = NamedTemporaryFile(
                delete=False,
                dir=lock_file.parent,
                prefix=prefix,
                suffix=suffix,
            )
            try:
                yield temp_file
            finally:
                temp_file.close()
                Path(temp_file.name).unlink(missing_ok=True)
        else:
            yield temp_file

    def mark_started(self) -> bool:
        """
        Report the worker has started up and is about to check for tasks to execute.
        """
        if self.lock_file is not None:
            # An OS that is not Windows can read the exact path from this locked file.
            # Windows OS reliably cleans up the *.tid file, so it should be able to find
            # it on the filesystem without reading this lock file (which it cannot).
            previous = None
            resolved = ""
            if self.task_file:
                resolved = str(self.task_file.resolve(strict=False))
            with suppress(OSError):
                previous = self.lock_file.read_text()
            with suppress(OSError):
                self.lock_file.write_text(resolved)
            f = self.running_stack.enter_context(self.lock_file.open("ab"))
            # Grab an exclusive lock (if possible)
            if not locks.lock(f, locks.LOCK_EX | locks.LOCK_NB):
                f.close()
                if previous is not None:
                    with suppress(OSError):
                        self.lock_file.write_text(previous)
                return False

        if self.sd_notify is not None:
            self.sd_notify.ready()

        if self.s6_notify is not None:
            self.s6_notify.ready()

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
        if self.task_file is not None:
            self.task_file.write_text(task_id)

        if self.sd_notify is not None:
            self.sd_notify.status(f"Running {task_id}")

        return True

    def mark_not_running_task(self) -> bool:
        """
        Report that the worker is not running a task anymore.
        """
        if self.task_file is not None:
            self.task_file.write_text("")

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
