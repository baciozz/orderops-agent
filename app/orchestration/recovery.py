"""Commit acknowledgement loss is reconciled with the original request and key."""

from app.db.models import Task
from app.db.session import begin_immediate
from app.observability import enrich, get_logger, observed, task_fields_from_factory
from app.orchestration.state import transition
from app.services.kernel import acknowledge_commit, commit_plan

logger = get_logger(__name__)


class ResponseLost(Exception):
    pass


@observed(
    logger,
    "recovery.durable_commit",
    start_event="recovery.durable_commit",
    exclude=("mode",),
    fields=lambda request, **_: {
        "plan_id": request.get("plan_id"),
        "approval_id": request.get("approval_id"),
        "replayed": None,
    },
)
def durable_commit(factory, *, fault=lambda _: None, **request):
    outcome = commit_plan(factory, acknowledge=False, **request)
    enrich(**task_fields_from_factory(factory, outcome.task_id), replayed=outcome.replayed)
    try:
        fault("after_commit")
    except ResponseLost:
        with factory() as session, begin_immediate(session):
            task = session.get(Task, outcome.task_id)
            # The business transaction is already durable. Only its acknowledgement is unknown.
            if task.status == "COMMITTING":
                transition(task, "RECONCILING")
        raise
    acknowledge_commit(factory, outcome.task_id)
    return outcome
