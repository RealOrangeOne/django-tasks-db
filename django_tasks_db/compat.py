from importlib.util import find_spec
from typing import TypeVar

from typing_extensions import ParamSpec

if find_spec("django.tasks"):
    from django.tasks import task_backends
    from django.tasks.backends.base import BaseTaskBackend
    from django.tasks.base import (
        DEFAULT_TASK_BACKEND_ALIAS,
        DEFAULT_TASK_PRIORITY,
        DEFAULT_TASK_QUEUE_NAME,
        TASK_MAX_PRIORITY,
        TASK_MIN_PRIORITY,
        Task,
        TaskContext,
        TaskError,
        TaskResultStatus,
    )
    from django.tasks.base import (
        TaskResult as BaseTaskResult,
    )
    from django.tasks.exceptions import InvalidTaskBackend, TaskResultDoesNotExist
    from django.tasks.signals import (
        task_enqueued,
        task_finished,
        task_started,
    )
    from django.utils.json import normalize_json

    TASKS_LOGGER = "django.tasks"
else:
    try:
        from typing import Generic

        from django_tasks import (  # type: ignore[assignment,no-redef,unused-ignore]
            task_backends,
        )
        from django_tasks.backends.base import (  # type: ignore[assignment,no-redef,unused-ignore]
            BaseTaskBackend,
        )
        from django_tasks.base import (  # type: ignore[assignment,no-redef,unused-ignore]
            DEFAULT_TASK_BACKEND_ALIAS,
            DEFAULT_TASK_PRIORITY,
            DEFAULT_TASK_QUEUE_NAME,
            TASK_MAX_PRIORITY,
            TASK_MIN_PRIORITY,
            Task,
            TaskContext,
            TaskError,
            TaskResultStatus,
        )
        from django_tasks.base import (
            TaskResult as _TaskResultWithoutP,
        )
        from django_tasks.exceptions import (  # type: ignore[assignment,no-redef,unused-ignore]
            InvalidTaskBackendError as InvalidTaskBackend,
        )
        from django_tasks.exceptions import (  # type: ignore[assignment,no-redef,unused-ignore]
            TaskResultDoesNotExist,
        )
        from django_tasks.signals import (  # type: ignore[no-redef,unused-ignore]
            task_enqueued,
            task_finished,
            task_started,
        )
        from django_tasks.utils import (  # type: ignore[no-redef,unused-ignore]
            normalize_json,
        )

        TASKS_LOGGER = "django_tasks"

        T = TypeVar("T")
        P = ParamSpec("P")

        class BaseTaskResult(_TaskResultWithoutP[T], Generic[P, T]):  # type: ignore[misc,no-redef,unused-ignore]
            pass
    except (ImportError, ModuleNotFoundError) as e:
        raise ValueError(
            "Either use Django 6+ or include django-tasks with [compat] extra"
        ) from e


__all__ = [
    "BaseTaskBackend",
    "BaseTaskResult",
    "BaseTaskResult",
    "DEFAULT_TASK_BACKEND_ALIAS",
    "DEFAULT_TASK_PRIORITY",
    "DEFAULT_TASK_QUEUE_NAME",
    "InvalidTaskBackend",
    "TASK_MAX_PRIORITY",
    "TASK_MIN_PRIORITY",
    "TASKS_LOGGER",
    "Task",
    "TaskContext",
    "TaskError",
    "TaskResultDoesNotExist",
    "TaskResultStatus",
    "normalize_json",
    "task_backends",
    "task_enqueued",
    "task_finished",
    "task_started",
]
