"""Task definition: the ``@task`` decorator and the :class:`Task` dataclass.

A task wraps a plain function together with its worker kind, upstream
dependencies, retry budget and timeout::

    @task(kind="io", retries=3, args=("https://example.com",))
    def fetch(url): ...

    @task(kind="cpu", after=[fetch])
    def analyse(data): ...

    @task(kind="async", after=[analyse])
    async def publish(report): ...

Calling convention when the scheduler runs a task::

    fn(*upstream_results, *task.args, **task.kwargs)

where ``upstream_results`` are the results of the tasks in ``after``, in the
order they were listed.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, get_args

__all__ = ["KINDS", "Kind", "Task", "TaskDefinitionError", "task"]

Kind: TypeAlias = Literal["io", "cpu", "async"]
KINDS: tuple[Kind, ...] = get_args(Kind)

Dependency: TypeAlias = "Task | str"


class TaskDefinitionError(ValueError):
    """Raised when a task is declared with invalid options."""


@dataclass(frozen=True, eq=False)
class Task:
    """A unit of work in a MiniFlow DAG.

    Tasks compare and hash by identity; within one DAG they are addressed by
    :attr:`name`, which must be unique.
    """

    fn: Callable[..., Any]
    name: str
    kind: Kind = "io"
    after: tuple[str, ...] = ()
    retries: int = 0
    timeout: float | None = None
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise TaskDefinitionError(
                f"task {self.name!r}: kind must be one of {KINDS}, got {self.kind!r}"
            )
        if self.retries < 0:
            raise TaskDefinitionError(f"task {self.name!r}: retries must be >= 0")
        if self.timeout is not None and self.timeout <= 0:
            raise TaskDefinitionError(f"task {self.name!r}: timeout must be > 0")
        if self.name in self.after:
            raise TaskDefinitionError(f"task {self.name!r} depends on itself")
        if len(set(self.after)) != len(self.after):
            raise TaskDefinitionError(f"task {self.name!r} lists a dependency twice")
        is_coro = inspect.iscoroutinefunction(self.fn)
        if self.kind == "async" and not is_coro:
            raise TaskDefinitionError(
                f"task {self.name!r}: kind='async' requires an 'async def' function"
            )
        if self.kind != "async" and is_coro:
            raise TaskDefinitionError(
                f"task {self.name!r}: coroutine functions must use kind='async'"
            )
        if self.kind == "cpu":
            _check_importable(self)

    @property
    def max_attempts(self) -> int:
        """Total number of attempts allowed (first run plus retries)."""
        return self.retries + 1

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped function directly, bypassing the scheduler."""
        return self.fn(*args, **kwargs)

    def __repr__(self) -> str:
        return f"Task({self.name!r}, kind={self.kind!r}, after={list(self.after)!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        # Process workers receive tasks by reference (module + qualname) so the
        # wrapped function is re-imported in the child rather than pickled.
        return (_load_task, (self.fn.__module__, self.fn.__qualname__))


def _load_task(module: str, qualname: str) -> Task:
    obj: Any = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, Task):
        raise TypeError(f"{module}.{qualname} is not a MiniFlow task")
    return obj


def _check_importable(t: Task) -> None:
    """cpu tasks run in another process, so the function must be importable there."""
    qualname = getattr(t.fn, "__qualname__", "")
    if "<lambda>" in qualname or "<locals>" in qualname:
        raise TaskDefinitionError(
            f"task {t.name!r}: kind='cpu' needs a module-level function so it can be "
            f"pickled for a worker process (got {qualname!r})"
        )


def _dep_name(dep: Dependency) -> str:
    if isinstance(dep, Task):
        return dep.name
    if isinstance(dep, str):
        return dep
    raise TaskDefinitionError(f"dependencies must be tasks or task names, got {dep!r}")


def task(
    fn: Callable[..., Any] | None = None,
    *,
    kind: Kind = "io",
    after: Iterable[Dependency] = (),
    retries: int = 0,
    timeout: float | None = None,
    name: str | None = None,
    args: Iterable[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Declare a task. Usable bare (``@task``) or with options (``@task(kind="cpu")``)."""

    def wrap(f: Callable[..., Any]) -> Task:
        return Task(
            fn=f,
            name=name or f.__name__,
            kind=kind,
            after=tuple(_dep_name(d) for d in after),
            retries=retries,
            timeout=timeout,
            args=tuple(args),
            kwargs=dict(kwargs or {}),
        )

    return wrap(fn) if fn is not None else wrap
