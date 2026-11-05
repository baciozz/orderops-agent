"""Small process-local counters derived from structured business events."""

from collections.abc import Mapping
from threading import Lock

_NAMES = (
    "tasks_enqueued",
    "tasks_succeeded",
    "tasks_failed",
    "approvals_granted",
    "commits_applied",
    "commits_replayed",
    "verification_pending_retries",
    "verification_pending_cleared",
    "watchdog_scans",
    "worker_claims",
    "model_calls",
    "budget_refused",
)
_lock = Lock()
_counters = dict.fromkeys(_NAMES, 0)


def record_event(event: str, fields: Mapping[str, object]) -> None:
    """Count emissions, independently of logging level, handlers or formatting."""
    names: list[str] = []
    outcome = fields.get("outcome")
    if event == "inbox.received" and outcome == "success" and fields.get("duplicate") is False:
        names.append("tasks_enqueued")
    elif event == "task.finished" and fields.get("status") in {"SUCCEEDED", "FAILED"}:
        names.append("tasks_succeeded" if fields["status"] == "SUCCEEDED" else "tasks_failed")
    elif event == "approval.granted" and outcome == "success":
        names.append("approvals_granted")
    elif event == "commit.applied" and outcome == "success":
        names.append("commits_replayed" if fields.get("replayed") else "commits_applied")
    elif event == "verification.retry":
        names.append("verification_pending_retries")
        if outcome == "cleared":
            names.append("verification_pending_cleared")
    elif event == "watchdog.scan" and outcome == "started":
        names.append("watchdog_scans")
    elif event == "lease.acquired" and outcome == "success":
        names.append("worker_claims")
    elif event == "model.call":
        names.append("model_calls")
    elif event == "budget.refused":
        names.append("budget_refused")
    with _lock:
        for name in names:
            _counters[name] += 1


def snapshot() -> dict[str, int]:
    """Return an independent, consistent snapshot, including zero-valued counters."""
    with _lock:
        return _counters.copy()


def reset_for_tests() -> None:
    with _lock:
        _counters.update(dict.fromkeys(_NAMES, 0))
