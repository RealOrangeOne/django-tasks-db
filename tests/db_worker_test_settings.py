from .settings import *

TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}

# Force the test DB to be used for all databases
for db in DATABASES.values():
    db = cast(dict[str, Any], db)
    if "sqlite" in db.get("ENGINE", ""):
        if "TEST" in db and "NAME" in db["TEST"]:
            db["NAME"] = db["TEST"]["NAME"]
    elif "NAME" in db:
        db["NAME"] = f"test_{db['NAME']}"
