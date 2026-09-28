import pytest
from django.db import connection
from due_work_harness import configure
from due_work_harness.integrations.django import django_host
from due_work_harness.integrations.django.receivers import django_receiver_breaker

from django_tasks_db.compat import task_finished, task_started

# The harness counts a worker's commits by asking PostgreSQL whether a statement
# wrote, so the contract runs only there.
if connection.vendor != "postgresql":
    raise pytest.UsageError(
        "the due-work contract needs PostgreSQL: set DATABASE_URL=postgres://..."
    )

# The system under test is django-tasks-db, running tasks for Django's task
# framework: bindings must reach one of them. Crash histories fail each receiver
# of the signals db_worker sends in turn.
configure(
    django_host(
        production_packages={"django_tasks_db", "django_tasks", "django"},
        lifecycle_proofs=False,
        receiver_breaker=django_receiver_breaker(task_started, task_finished),
    )
)
