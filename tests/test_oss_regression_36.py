import logging
from unittest.mock import patch

import django
from django.conf import settings
from django.test import SimpleTestCase

from tests import settings as test_settings


def setUpModule() -> None:
    if not settings.configured:
        settings.configure(
            INSTALLED_APPS=test_settings.INSTALLED_APPS,
            TASKS=test_settings.TASKS,
        )
        django.setup()


class WorkerLoggingTests(SimpleTestCase):
    def test_worker_id_is_available_during_worker_run(self) -> None:
        from django_tasks_db.management.commands.db_worker import Command, Worker

        application_logger = logging.getLogger("example.application")

        def run_task() -> None:
            application_logger.info("Task running")

        with (
            self.assertLogs(application_logger) as captured,
            patch.object(Worker, "configure_signals"),
            patch.object(Worker, "run", side_effect=run_task),
        ):
            Command().handle(
                verbosity=None,
                queue_name="default",
                interval=0,
                batch=True,
                backend_name="default",
                startup_delay=False,
                reload=False,
                max_tasks=None,
                worker_id="worker-36",
                exclude_queues="",
                log_worker_id=True,
            )
            application_logger.info("Outside worker")

        assert [getattr(record, "worker_id", None) for record in captured.records] == [
            "worker-36",
            None,
        ]
