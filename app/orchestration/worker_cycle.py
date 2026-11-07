"""One existing watchdog → worker → local outbox cycle, without scheduling."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.orchestration import outbox, watchdog, worker
from app.orchestration.dispatch import DispatchOutcome
from app.orchestration.worker import DEFAULT_LIMITS, Limits, effective_active


@dataclass(frozen=True)
class CycleResult:
    recovered: list[str]
    outcome: DispatchOutcome | None
    published: int


def run_cycle(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    clock: Callable[[], datetime] | None = None,
    fixture_only: bool = True,
    limits: Limits | None = None,
) -> CycleResult:
    # Both CLI entry points default to FIXTURE. Other callers must explicitly
    # opt into live consumption and still obey each task's persistent mode.
    timing: dict[str, Any] = {} if clock is None else {"clock": clock}
    cycle_settings = (
        settings.model_copy(update={"live_enabled": False}) if fixture_only else settings
    )
    dispatch: dict[str, Any] = {"settings": cycle_settings}
    recovered = watchdog.scan(factory, settings=cycle_settings, **timing)
    if limits is None:
        limits = replace(DEFAULT_LIMITS, active=effective_active(settings.worker_max_active))
    outcome = worker.run_once(factory, limits=limits, **timing, **dispatch)
    published = outbox.publish_pending(factory, **timing)
    return CycleResult(recovered, outcome, published)
