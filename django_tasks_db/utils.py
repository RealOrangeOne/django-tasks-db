import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar
from uuid import UUID

from django.db import OperationalError, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from typing_extensions import ParamSpec

T = TypeVar("T")
P = ParamSpec("P")


@contextmanager
def exclusive_transaction(using: str | None = None) -> Generator[Any, Any, Any]:
    """
    Wrapper around `transaction.atomic` which ensures transactions on SQLite are exclusive.
    """
    connection: BaseDatabaseWrapper = transaction.get_connection(using)

    if connection.vendor == "sqlite":
        if not hasattr(connection, "transaction_mode"):
            # Manually called to set `transaction_mode`
            connection.get_connection_params()

        old_transaction_mode = connection.transaction_mode  # type: ignore[attr-defined]
        try:
            connection.transaction_mode = "EXCLUSIVE"  # type: ignore[attr-defined]
            with transaction.atomic(using=using):
                yield
        finally:
            connection.transaction_mode = old_transaction_mode  # type: ignore[attr-defined]
    else:
        with transaction.atomic(using=using):
            yield


def normalize_uuid(val: str | UUID) -> str:
    """
    Normalize a UUID into its dashed representation.

    This works around engines like MySQL which don't store values in a uuid field,
    and thus drops the dashes.
    """
    if isinstance(val, str):
        val = UUID(val)

    return str(val)


def retry(*, retries: int = 3, backoff_delay: float = 0.1) -> Callable:
    """
    Retry the given code `retries` times, raising the final error.

    `backoff_delay` can be used to add a delay between attempts.
    """

    def wrapper(f: Callable[P, T]) -> Callable[P, T]:
        @wraps(f)
        def inner_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:  # type:ignore[return]
            for attempt in range(1, retries + 1):
                try:
                    return f(*args, **kwargs)
                except KeyboardInterrupt:
                    # Let the user ctrl-C out of the program without a retry
                    raise
                except BaseException:
                    if attempt == retries:
                        raise
                    time.sleep(backoff_delay)

        return inner_wrapper

    return wrapper


def is_locked_database_exception(e: OperationalError) -> bool:
    if isinstance(e.args[0], str) and "is locked" in e.args[0].lower():
        return True

    # MySQL has an error code in the first argument, and message in the second
    elif isinstance(e.args[0], int) and e.args[0] == 1205:
        return True

    return False
