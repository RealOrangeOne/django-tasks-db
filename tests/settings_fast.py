from .settings import *

# Unset custom test settings to use in-memory DB
for db in DATABASES.values():
    if "sqlite" in db["ENGINE"] and "TEST" in db:
        del db["TEST"]
