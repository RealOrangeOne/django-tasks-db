_task_classes = []

try:
    from django.tasks.base import Task as DjangoTask

    _task_classes.append(DjangoTask)
except ImportError:
    pass

try:
    from django_tasks.base import Task

    _task_classes.append(Task)
except ImportError:
    pass

__all__ = ["TASK_CLASSES"]

TASK_CLASSES = tuple(_task_classes)
