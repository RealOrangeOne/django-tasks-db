"""
django-tasks-db's contract with its worker, proven by due-work-harness.

A task enqueued with the database backend is a row, committed or rolled back
with the transaction that enqueued it. The worker, ``manage.py db_worker``,
claims a READY row, marks it RUNNING, runs the task and records how it ended.
The obligation this contract states: a task that ran is recorded as it ended,
and a task that has not run is run.

``worker_contract`` from due-work-harness's django-tasks integration generates
the proofs, bound here to ``tests.tasks.send_message``, whose effect is a
message sent outside the database. Retention is claimed and proven against
``prune_db_task_results``. What the worker lacks is declared as known gaps,
each a strict xfail that a fix flips to a failure: no reclaim of a task whose
worker died (#5), no lease, no attempt record. The worker's crash histories
kill it right after each commit and right after the task's external call, and
fail each ``task_started`` and ``task_finished`` receiver in turn;
``test_what_each_failure_costs`` pins what every one of those histories
leaves, including the task_finished receiver that rewrites a finished task
as FAILED (#62).

To run it (Python 3.12 or later, PostgreSQL)::

    pip install -e . --group dev --group postgres
    DATABASE_URL=postgres://postgres:postgres@localhost:15432/postgres python -m pytest

or ``just start-dbs test-due-work``. Settings and test paths come from
``[tool.pytest.ini_options]`` in pyproject.toml.
"""

from due_work_harness import (
    CallableDelivery,
    assert_pinned_outcomes,
    due_work_contract_suite,
    due_work_database,
)
from due_work_harness.crash_histories import ExternalCall
from due_work_harness.integrations.django_tasks import (
    TaskOutcome,
    db_worker_once,
    worker_contract,
)

from tests.tasks import Outbox, outbox, send_message

MESSAGE = "your order has shipped"

# REAL PRODUCTION: one `manage.py db_worker --batch` pass, as a deployment runs the worker.
WORKER = CallableDelivery(name="django-tasks-db", recover=db_worker_once())


def a_message_owed() -> str:
    # ARRANGE: a task that sends one message, enqueued and ready for a worker.
    outbox.sent.clear()
    return str(send_message.enqueue(MESSAGE).id)


def messages_sent(_task_id: str) -> int:
    # OBSERVE: how many times the message left for the outside system.
    return outbox.sent[MESSAGE]


CONTRACT = worker_contract(
    name="django-tasks-db: the worker running a task",
    enqueue=a_message_owed,
    effect=messages_sent,
    # EXTERNAL SEAM: the outbox the task sends its message through.
    external_calls=(ExternalCall(owner=Outbox, attribute="send"),),
    delivery=WORKER,
)
WORKER_RUNS_A_TASK = CONTRACT.handoffs[0]


# This is where the magic happens. The class is empty on purpose: the decorator reads
# CONTRACT and generates its tests, bound to the real `db_worker`, `prune_db_task_results`
# and send_message task. No test case is written by hand, and this file supplies only the
# task and how to see its effect; the guarantees and their proofs are the integration's.
#
# One of the generated cases is how #62 was found: the handoff case replays one db_worker
# run per thing that can go wrong (the worker dies after each commit or after the message
# is sent, or a task_started or task_finished receiver raises) and compares each run with
# a normal one. When a task_finished receiver raises, the message was sent but the task is
# recorded FAILED, so the case fails. The contract declares that as a gap, so it is
# reported as a strict XFAIL; the day every run matches, it passes, and the strict marker
# fails the run until the gap is removed.
@due_work_contract_suite(CONTRACT)
class TestWorkerContract:
    """Every case in this class is generated from CONTRACT; see the comment above."""


def _task(status: str, sent: int) -> TaskOutcome:
    return TaskOutcome(status=status, effect=sent)


# What each history leaves after the worker runs again, pinned. A change in the
# worker moves an entry, and the test names the one that moved. The receivers
# failed are the task framework's own logging receivers, standing in for any
# receiver that raises (a bug, an unreachable metrics backend).
WORKER_FINDINGS: dict[str, TaskOutcome] = {
    # FINDING (#5): the claim committed, the worker died, and the task stays RUNNING forever.
    "worker died after commit 1": _task("RUNNING", 0),
    # Benign: set_successful committed the outcome before the death (commit 3 is the next, empty, poll).
    "worker died after commit 2": _task("SUCCESSFUL", 1),
    "worker died after commit 3": _task("SUCCESSFUL", 1),
    # FINDING (#5): the message was sent and the task stays RUNNING forever.
    "worker died after external call 1": _task("RUNNING", 1),
    # FINDING: task_started's receiver raised, so the task is FAILED without running, and nothing retries it.
    "signal receiver 1 failed": _task("FAILED", 0),
    # FINDING (#62): task_finished's receiver raised after the task ran, and its SUCCESSFUL record was
    # rewritten as FAILED although the message was sent.
    "signal receiver 2 failed": _task("FAILED", 1),
}


@due_work_database()
def test_what_each_failure_costs() -> None:
    assert_pinned_outcomes(
        WORKER,
        WORKER_RUNS_A_TASK,
        delivered=_task("SUCCESSFUL", 1),
        outcomes=WORKER_FINDINGS,
    )
