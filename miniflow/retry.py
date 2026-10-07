"""Retry policy with exponential backoff and jitter (spec §3.4, §3.6 "Retries").

Contract (Phase 2, module B)
----------------------------
``@dataclass(frozen=True) RetryPolicy(base: float = 0.1, cap: float = 30.0, jitter: float = 0.1)``
    ``delay(attempt: int, rng: random.Random | None = None) -> float``
        Seconds to wait after failed attempt number ``attempt`` (1-based):
        ``min(cap, base * 2 ** (attempt - 1)) + uniform(0, jitter * that)``.
        Never negative; deterministic when given a seeded ``rng``.
    ``should_retry(task: Task, attempt: int) -> bool``
        True while ``attempt < task.max_attempts``.

``class TaskTimeoutError(TimeoutError)``
    Raised (or recorded) when a task exceeds ``task.timeout``.

``deadline(seconds: float | None) -> ContextManager[Deadline]``
    Context manager yielding an object with ``.remaining() -> float | None``
    and ``.expired() -> bool``, used by the scheduler to bound waits.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from miniflow.task import Task

__all__ = ["Deadline", "RetryPolicy", "TaskTimeoutError", "deadline"]

# Shared fallback generator for callers that don't pass their own ``rng``.
_RNG = random.Random()


class TaskTimeoutError(TimeoutError):
    """A task ran longer than its ``timeout``."""


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with proportional jitter.

    The delay after failed attempt ``n`` is ``base * 2 ** (n - 1)``, capped at
    ``cap``, plus a random extra of up to ``jitter`` times that amount.
    """

    base: float = 0.1
    cap: float = 30.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        for name in ("base", "cap", "jitter"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"RetryPolicy.{name} must be a finite number >= 0, got {value!r}")

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        try:
            raw = self.base * 2.0 ** (attempt - 1)
        except OverflowError:
            raw = math.inf
        backoff = min(self.cap, raw)
        if self.jitter == 0 or backoff == 0:
            return backoff
        return backoff + (rng or _RNG).uniform(0.0, self.jitter * backoff)

    def should_retry(self, task: Task, attempt: int) -> bool:
        """True while ``attempt`` (1-based, just failed) leaves budget for another."""
        return attempt < task.max_attempts


class Deadline:
    """A point in monotonic time after which a wait should give up.

    ``seconds=None`` means no deadline: :meth:`remaining` returns ``None`` and
    :meth:`expired` is always ``False``.
    """

    __slots__ = ("_end", "seconds")

    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds
        self._end = None if seconds is None else time.monotonic() + seconds

    def remaining(self) -> float | None:
        """Seconds left (never negative), or ``None`` when unbounded."""
        if self._end is None or self.seconds is None:
            return None
        # Clamp to the budget: float rounding in ``(start + s) - now`` can exceed ``s``.
        return max(0.0, min(self.seconds, self._end - time.monotonic()))

    def expired(self) -> bool:
        """True once the deadline has passed."""
        return self._end is not None and time.monotonic() >= self._end

    def __repr__(self) -> str:
        return f"Deadline(seconds={self.seconds!r}, remaining={self.remaining()!r})"


@contextmanager
def deadline(seconds: float | None) -> Iterator[Deadline]:
    """Start a :class:`Deadline` of ``seconds`` from now and yield it."""
    yield Deadline(seconds)
