"""
The worker's due-work contract, run by pytest with due-work-harness (`just test-due-work`).

Django's test runner skips this package: its tests are pytest tests, and
due-work-harness needs Python 3.12 or later and PostgreSQL.
"""

import unittest


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    return unittest.TestSuite()
