import os
import sys

import dj_database_url

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IN_TEST = "IN_TEST" in os.environ or (len(sys.argv) > 1 and sys.argv[1] == "test")

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django_tasks_db",
    "tests",
]

SECRET_KEY = "abcde12345"

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

DATABASES = {
    "default": dj_database_url.config(
        default="sqlite:///" + os.path.join(BASE_DIR, "db.sqlite3")
    )
}

for db in DATABASES.values():
    if "sqlite" in db["ENGINE"]:
        db["TEST"] = {
            "NAME": os.path.join(BASE_DIR, f"db-test-{os.path.basename(db['NAME'])}")
        }


USE_TZ = True

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "QUEUES": ["default", "queue-1"],
    }
}

if not IN_TEST:
    DEBUG = True
