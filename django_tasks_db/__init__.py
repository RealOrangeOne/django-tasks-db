# ruff: noqa: E402
import django_stubs_ext

django_stubs_ext.monkeypatch()

import importlib.metadata

__version__ = importlib.metadata.version(__name__)

from .backend import DatabaseBackend

__all__ = ["DatabaseBackend"]
