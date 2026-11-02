"""Commit acknowledgement loss is reconciled with the original request and key."""

from app.db.models import Task
from app.db.session import begin_immediate
from app.orchestration.state import transition
from app.services.kernel import commit_plan


class ResponseLost(Exception):
    pass


def durable_commit(factory, *, fault=lambda _: None, **request):
    outcome = commit_plan(factory, **request)
    try:
        fault("after_commit")
    except ResponseLost:
        with factory() as session, begin_immediate(session):
            task = session.get(Task, outcome.task_id)
            # The business transaction is already durable. Only its acknowledgement is unknown.
            transition(task, "RECONCILING")
        raise
    with factory() as session, begin_immediate(session):
        task = session.get(Task, outcome.task_id)
        if task.status == "RECONCILING":
            transition(task, "SUCCEEDED")
    return outcome
