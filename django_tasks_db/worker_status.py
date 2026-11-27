from contextlib import ExitStack
from pathlib import Path

from django.core.files import locks


class WorkerStatus:
    """
    A class to help db_worker report its status.
    """

    def __init__(self, *, worker_id: str, lock_file: Path | None = None) -> None:
        self.worker_id = worker_id
        self.lock_file = lock_file

        self.running_stack = ExitStack()

    def mark_started(self) -> bool:
        """
        Report the worker has started up and is about to check for tasks to execute.
        """
        if self.lock_file is not None:
            f = self.running_stack.enter_context(self.lock_file.open("ab"))
            # Grab an exclusive lock (if possible)
            if not locks.lock(f, locks.LOCK_EX | locks.LOCK_NB):
                return False

        return True

    def mark_stopped(self) -> bool:
        """
        Report the worker has stopped (gracefully).
        """
        self.running_stack.close()
        if self.lock_file is not None:
            # Clean up the lock file
            self.lock_file.unlink()
        return True
