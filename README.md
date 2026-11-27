# Django Tasks DB

[![CI](https://github.com/RealOrangeOne/django-tasks-db/actions/workflows/ci.yml/badge.svg)](https://github.com/RealOrangeOne/django-tasks-db/actions/workflows/ci.yml)
![PyPI](https://img.shields.io/pypi/v/django-tasks-db.svg)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/django-tasks-db.svg)
![PyPI - Status](https://img.shields.io/pypi/status/django-tasks-db.svg)
![PyPI - License](https://img.shields.io/pypi/l/django-tasks-db.svg)


A [Django Tasks](https://docs.djangoproject.com/en/stable/topics/tasks/) backend which uses Django's ORM to store tasks in the database.

## Installation

```
python -m pip install django-tasks-db
```

First, add `django_tasks_db` to your `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "django_tasks_db",
]
```

Finally, add it to your `TASKS` configuration:

```python
TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "QUEUES": ["default"]
    }
}
```

## Usage

### Worker

You can run the `db_worker` command to run tasks as they're created. Check the `--help` for more options.

```shell
./manage.py db_worker
```

In `DEBUG`, the worker will automatically reload when code is changed (or by using `--reload`). This is not recommended in production environments as tasks may not be stopped cleanly.

#### Worker health

It may be useful in your environment to check the health of the worker, more so than just whether the process is running.

##### Lock file

A lock file can be provided using `--lock-file`. This file will be locked exclusively whilst the worker is running. Multiple workers cannot use the same lock file. The lock file will be deleted when the worker exits successfully. It's recommended to store the lock file in-memory (eg `tmpfs`) to reduce writes on persistent storage.

The lock file's modified time will be updated (`touch`) each time the worker checks for a task. This can be used to indicate that the loop is still running.

When a worker is running a task, the task's id is written to the text file. This does not work on Windows.

To check the health of the worker using the lock file, use the `db_worker_healthcheck` management command, and provide the lock file's path. You can also pass `--max-age` to ensure the worker has `touch`'d the lock file in at least this many seconds.

### Pruning old tasks

After a while, tasks may start to build up in your database. This can be managed using the `prune_db_task_results` management command, which deletes completed tasks according to the given retention policy. Check the `--help` for the available options.

### Customizing the task id

By default, the database worker uses `uuid.uuid4` to generate a task id. This can be customized using the `id_function` option:

```python
TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "OPTIONS": {
            "id_function": "uuid.uuid7"
        }
    }
}
```

The `id_function` must return a UUID (either `uuid.UUID` or string representation). Additionally, the PostgreSQL-specific [`RandomUUID`](https://docs.djangoproject.com/en/stable/ref/contrib/postgres/functions/#django.contrib.postgres.functions.RandomUUID) or other database expressions are supported on Django 6.0+.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for information on how to contribute.

<a href="https://github.com/realorangeone/django-tasks-db/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=realorangeone/django-tasks-db" />
</a>

Note: Prior to `0.12.0`, this backend was included in [`django-tasks`](https://github.com/RealOrangeOne/django-tasks/). Whilst the commit history was cleaned up, it's still quite messy. Don't look too closely.
