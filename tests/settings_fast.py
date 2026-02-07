from .settings import *

# Unset custom test settings to use in-memory DB
if "sqlite" in DATABASES["default"]["ENGINE"]:  # type: ignore[operator]
    del DATABASES["default"]["TEST"]  # type: ignore[attr-defined]
