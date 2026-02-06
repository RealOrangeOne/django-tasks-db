import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar
from uuid import UUID

import django
from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from typing_extensions import ParamSpec

T = TypeVar("T")
P = ParamSpec("P")


def connection_requires_manual_exclusive_transaction(
    connection: BaseDatabaseWrapper,
) -> bool:
    """
    Determine whether the backend requires manual transaction handling.

    Extracted from `exclusive_transaction` for unit testing purposes.
    """
    if connection.vendor != "sqlite":
        return False

    if django.VERSION < (5, 1):
        return True

    if not hasattr(connection, "transaction_mode"):
        # Manually called to set `transaction_mode`
        connection.get_connection_params()

    return connection.transaction_mode != "EXCLUSIVE"  # type:ignore[attr-defined,no-any-return]


@contextmanager
def exclusive_transaction(using: str | None = None) -> Generator[Any, Any, Any]:
    """
    Wrapper around `transaction.atomic` which ensures transactions on SQLite are exclusive.

    This functionality is built-in to Django 5.1+.
    """
    connection: BaseDatabaseWrapper = transaction.get_connection(using)

    if connection_requires_manual_exclusive_transaction(connection):
        with connection.cursor() as c:
            c.execute("BEGIN EXCLUSIVE")
            try:
                yield
            finally:
                c.execute("COMMIT")
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
