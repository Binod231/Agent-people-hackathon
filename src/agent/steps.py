"""Step timing and the deadline guard.

Two jobs. `StepRecorder` times each pipeline step and keeps the record that
every response carries, success or failure (Requirement 5 AC7, Property 15).
`Deadline` answers one question -- is there time left for this -- so the handler
can return a `DEADLINE` message instead of letting API Gateway drop the
connection (Requirements 4 AC6, 5 AC6).

`StepName`, `StepRecord`, and `Deadline` are defined in `models.py` with the
other frozen dataclasses and re-exported here, so the whole step vocabulary can
be imported from one module.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .models import Deadline, StepName, StepRecord

__all__ = [
    "STEP_ORDER",
    "Deadline",
    "StepName",
    "StepRecord",
    "StepRecorder",
]

# Execution order, taken from the enum's declaration order. `ui/app.js` holds
# the same sequence with display labels and latency hints; the two must match.
STEP_ORDER: tuple[StepName, ...] = tuple(StepName)


class StepRecorder:
    """Append-only record of which steps ran, how they ended, and how long.

    The recorder never swallows an exception and never raises one of its own. A
    step that fails is recorded with status "error" and the exception propagates
    untouched, which is what lets a failed response still show how far the run
    got (Property 15).

    `clock` is injected so tests assert durations against a fake monotonic
    counter rather than real elapsed time.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._records: list[StepRecord] = []

    @property
    def steps(self) -> tuple[StepRecord, ...]:
        return tuple(self._records)

    @property
    def total_ms(self) -> int:
        return sum(record.ms for record in self._records)

    @contextmanager
    def step(self, name: StepName) -> Iterator[None]:
        """Time one step. Records "ok", or "error" if the body raises."""
        started = self._clock()
        try:
            yield
        except BaseException:
            self._append(name, "error", started)
            raise
        else:
            self._append(name, "ok", started)

    def skip(self, name: StepName) -> None:
        """Record a step that was never reached."""
        self._records.append(StepRecord(name=name, status="skipped", ms=0))

    def as_dicts(self) -> list[dict[str, str | int]]:
        """The `steps` array of the response envelope."""
        return [record.as_dict() for record in self._records]

    def _append(self, name: StepName, status: str, started: float) -> None:
        elapsed_ms = int(round((self._clock() - started) * 1000.0))
        self._records.append(StepRecord(name=name, status=status, ms=max(0, elapsed_ms)))
