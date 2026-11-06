"""Explicit, bounded, serial FIXTURE cycles. No background service or auto-start."""

import math
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from time import monotonic, sleep
from types import FrameType
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.observability import exception_code, get_logger, log_event
from app.orchestration.state import utcnow
from app.orchestration.worker import DEFAULT_LIMITS, effective_active
from app.orchestration.worker_cycle import run_cycle

logger = get_logger(__name__)


class StopToken(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, seconds: float) -> object: ...


class StopFlag:
    """Main-thread signal flag: no threading locks in the signal handler.

    Idle shutdown detection uses at most 100ms sleep slices (plus OS scheduling
    delay). Inject monotonic/sleep for deterministic tests without real waiting.
    """

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        sleep_for: Callable[[float], object] = sleep,
    ) -> None:
        self._requested = False
        self._monotonic = monotonic_clock
        self._sleep = sleep_for

    def set(self) -> None:
        self._requested = True

    def is_set(self) -> bool:
        return self._requested

    def wait(self, seconds: float) -> bool:
        deadline = self._monotonic() + seconds
        while not self._requested:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(0.1, remaining))
        return True


@dataclass(frozen=True)
class LoopOptions:
    interval_seconds: float = 5.0
    max_iterations: int = 60

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (float, int))
            or not 0.1 <= self.interval_seconds <= 3600
            or not math.isfinite(self.interval_seconds)
        ):
            raise ValueError("interval_seconds must be finite and between 0.1 and 3600")
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or not 1 <= self.max_iterations <= 10000
        ):
            raise ValueError("max_iterations must be an integer between 1 and 10000")


DEFAULT_OPTIONS = LoopOptions()


@dataclass(frozen=True)
class LoopResult:
    iterations: int
    reason: str


@contextmanager
def shutdown_signals(stop: StopFlag) -> Iterator[None]:
    """CLI main-thread only; finish the current cycle rather than interrupt a write."""

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_loop(
    factory: sessionmaker[Session],
    settings: Settings,
    *,
    options: LoopOptions = DEFAULT_OPTIONS,
    clock: Callable[[], datetime] = utcnow,
    stop: StopToken | None = None,
    wait: Callable[[float], object] | None = None,
) -> LoopResult:
    """Wait a full interval after every non-final cycle, including an empty one.

    Clock, wait and stop are injectable; no child threads or overlapping cycles are
    created. Unhandled failures propagate immediately. Existing component-level
    durable retry policies remain responsible for recoverable delivery/readback.
    """
    stop = stop if stop is not None else StopFlag()
    wait = wait if wait is not None else stop.wait
    limits = replace(DEFAULT_LIMITS, active=effective_active(settings.worker_max_active))
    completed = 0
    log_event(
        logger,
        "worker_loop.started",
        mode="FIXTURE",
        interval_seconds=options.interval_seconds,
        max_iterations=options.max_iterations,
    )
    try:
        while completed < options.max_iterations and not stop.is_set():
            result = run_cycle(factory, settings, clock=clock, fixture_only=True, limits=limits)
            completed += 1
            log_event(
                logger,
                "worker_loop.cycle",
                mode="FIXTURE",
                iteration=completed,
                task_claimed=result.outcome is not None,
                recovered_tasks=len(result.recovered),
                published=result.published,
            )
            if completed == options.max_iterations or stop.is_set():
                break
            log_event(
                logger,
                "worker_loop.wait",
                mode="FIXTURE",
                iteration=completed,
                interval_seconds=options.interval_seconds,
                task_claimed=result.outcome is not None,
            )
            wait(options.interval_seconds)
    except Exception as exc:
        log_event(
            logger,
            "worker_loop.failed",
            mode="FIXTURE",
            iterations=completed,
            error_code=exception_code(exc),
            outcome="failed",
        )
        raise
    result_summary = LoopResult(completed, "stop_requested" if stop.is_set() else "max_iterations")
    log_event(
        logger,
        "worker_loop.stopped",
        mode="FIXTURE",
        iterations=completed,
        reason=result_summary.reason,
    )
    return result_summary
