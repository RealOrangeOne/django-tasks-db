# Django Tasks DB

[![CI](https://github.com/RealOrangeOne/django-tasks-db/actions/workflows/ci.yml/badge.svg)](https://github.com/RealOrangeOne/django-tasks-db/actions/workflows/ci.yml)
![PyPI](https://img.shields.io/pypi/v/django-tasks-db.svg)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/django-tasks-db.svg)
![PyPI - Status](https://img.shields.io/pypi/status/django-tasks-db.svg)
![PyPI - License](https://img.shields.io/pypi/l/django-tasks-db.svg)


A [Django Tasks](https://docs.djangoproject.com/en/stable/topics/tasks/) backend which uses Django's ORM to store tasks in the database.

## Installation

```
python -m pip install django-tasks
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

Note: Prior to `0.12.0`, this backend was included in [`django-tasks`](https://github.com/RealOrangeOne/django-tasks/). Whilst the commit history was cleaned up, it's still quite messy. Don't look too closely.
