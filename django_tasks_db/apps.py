from django.apps import AppConfig


class TasksAppConfig(AppConfig):
    name = "django_tasks_db"
    label = "django_tasks_database"
    verbose_name = "Tasks Database Backend"

    def ready(self) -> None:
        from . import signal_handlers  # noqa
