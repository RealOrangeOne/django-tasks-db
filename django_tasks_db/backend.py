from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from django import VERSION
from django.apps import apps
from django.core import checks
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db.models import Expression
from django.utils.module_loading import import_string
from django.utils.version import PY311
from typing_extensions import ParamSpec

from .compat import (
    BaseTaskBackend,
    BaseTaskResult,
    Task,
    TaskResultDoesNotExist,
    normalize_json,
    task_enqueued,
)

if TYPE_CHECKING:
    from .models import DBTaskResult

T = TypeVar("T")
P = ParamSpec("P")


@dataclass(frozen=True, slots=PY311, kw_only=True)  # type: ignore[literal-required]
class TaskResult(BaseTaskResult[P, T]):  # type: ignore[misc]
    db_result: "DBTaskResult"


class DatabaseBackend(BaseTaskBackend):
    supports_async_task = True
    supports_get_result = True
    supports_defer = True
    supports_priority = True

    def __init__(self, alias: str, params: dict) -> None:
        super().__init__(alias, params)

        if id_function := self.options.get("id_function"):
            if callable(id_function):
                self.id_function = id_function
            else:
                self.id_function = import_string(id_function)
        else:
            self.id_function = None

    def _get_id(self) -> Any:
        if self.id_function is None:
            # Defer model import to avoid AppRegistryNotReady when the
            # backend is instantiated before apps are fully loaded (e.g.
            # by @task() decorators in third-party packages).
            from .models import DBTaskResult

            # Fall back to the default defined on the model
            self.id_function = DBTaskResult._meta.pk.default

        result_id = self.id_function()

        if VERSION < (6, 0) and isinstance(result_id, Expression):
            raise ImproperlyConfigured(
                "id_function cannot be a database expression until Django 6.0"
            )

        return result_id

    def _task_to_db_task(
        self,
        task: Task[P, T],
        args: P.args,  # type:ignore[valid-type]
        kwargs: P.kwargs,  # type:ignore[valid-type]
    ) -> "DBTaskResult":
        from .models import DBTaskResult

        return DBTaskResult.objects.create(
            id=self._get_id(),
            args_kwargs=normalize_json({"args": args, "kwargs": kwargs}),
            priority=task.priority,
            task_path=task.module_path,
            queue_name=task.queue_name,
            run_after=task.run_after,  # type: ignore[misc]
            backend_name=self.alias,
        )

    async def _atask_to_db_task(
        self,
        task: Task[P, T],
        args: P.args,  # type:ignore[valid-type]
        kwargs: P.kwargs,  # type:ignore[valid-type]
    ) -> "DBTaskResult":
        from .models import DBTaskResult

        return await DBTaskResult.objects.acreate(
            id=self._get_id(),
            args_kwargs=normalize_json({"args": args, "kwargs": kwargs}),
            priority=task.priority,
            task_path=task.module_path,
            queue_name=task.queue_name,
            run_after=task.run_after,  # type: ignore[misc]
            backend_name=self.alias,
        )

    def enqueue(
        self,
        task: Task[P, T],
        args: P.args,  # type:ignore[valid-type]
        kwargs: P.kwargs,  # type:ignore[valid-type]
    ) -> TaskResult[P, T]:
        self.validate_task(task)

        db_result = self._task_to_db_task(task, args, kwargs)

        task_enqueued.send(type(self), task_result=db_result.task_result)

        return db_result.task_result

    async def aenqueue(
        self,
        task: Task[P, T],
        args: P.args,  # type:ignore[valid-type]
        kwargs: P.kwargs,  #  type:ignore[valid-type]
    ) -> TaskResult[P, T]:
        self.validate_task(task)

        db_result = await self._atask_to_db_task(task, args, kwargs)

        await task_enqueued.asend(type(self), task_result=db_result.task_result)

        return db_result.task_result

    def get_result(self, result_id: str) -> TaskResult:
        from .models import DBTaskResult

        try:
            return DBTaskResult.objects.get(id=result_id).task_result
        except (DBTaskResult.DoesNotExist, ValidationError) as e:
            raise TaskResultDoesNotExist(result_id) from e

    async def aget_result(self, result_id: str) -> TaskResult:
        from .models import DBTaskResult

        try:
            return (await DBTaskResult.objects.aget(id=result_id)).task_result
        except (DBTaskResult.DoesNotExist, ValidationError) as e:
            raise TaskResultDoesNotExist(result_id) from e

    def check(self, **kwargs: Any) -> list[checks.CheckMessage]:
        if apps.is_installed("django_tasks_db"):
            return super().check(**kwargs)
        else:
            backend_name = self.__class__.__name__
            return [
                checks.Error(
                    f"{backend_name} configured as django_tasks_db backend, but database app not installed",
                    "Insert 'django_tasks_db' in INSTALLED_APPS",
                ),
            ]
