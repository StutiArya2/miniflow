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

__all__ = ["RetryPolicy", "TaskTimeoutError"]


class TaskTimeoutError(TimeoutError):
    """A task ran longer than its ``timeout``."""


class RetryPolicy:
    """Placeholder until Phase 2 (module B)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("retry.py is implemented in Phase 2 (module B)")
