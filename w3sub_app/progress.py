"""Counted operation progress shared by services and worker queues."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProgressUpdate:
    phase: str
    completed: int
    total: int | None


ProgressCallback = Callable[[ProgressUpdate], None]


def report_progress(callback: ProgressCallback | None, phase: str,
                    completed: int, total: int | None) -> None:
    """Report finished work units when an observer is supplied."""
    if callback is None:
        return
    if completed < 0 or (total is not None and (total <= 0 or completed > total)):
        raise ValueError("Progress counts must be nonnegative and within a positive total")
    callback(ProgressUpdate(phase, completed, total))
