from .settings import *

TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}

# Force the test DB to be used
if "sqlite" in DATABASES["default"]["ENGINE"]:  # type: ignore[operator]
    DATABASES["default"]["NAME"] = DATABASES["default"]["TEST"]["NAME"]  # type: ignore[index]
else:
    DATABASES["default"]["NAME"] = "test_" + DATABASES["default"]["NAME"]  # type: ignore[index, operator]
