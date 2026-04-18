from .settings import *

# Unset custom test settings to use in-memory DB
for db in DATABASES.values():
    if "sqlite" in db["ENGINE"] and "TEST" in db:  # type: ignore[operator]
        del db["TEST"]  # type: ignore[attr-defined]
